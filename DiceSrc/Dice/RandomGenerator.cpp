/*
 *  _______     ________    ________    ________    __
 * |   __  \   |__    __|  |   _____|  |   _____|  |  |
 * |  |  |  |     |  |     |  |        |  |_____   |  |
 * |  |  |  |     |  |     |  |        |   _____|  |__|
 * |  |__|  |   __|  |__   |  |_____   |  |_____    __
 * |_______/   |________|  |________|  |________|  |__|
 *
 * Dice! QQ Dice Robot for TRPG
 * Copyright (C) 2018-2019 w4123溯洄
 *
 * This program is free software: you can redistribute it and/or modify it under the terms
 * of the GNU Affero General Public License as published by the Free Software Foundation,
 * either version 3 of the License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
 * without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License along with this
 * program. If not, see <http://www.gnu.org/licenses/>.
 */
#include "RandomGenerator.h"
#include "DiceNetwork.h"
#include "json.hpp"
#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <ctime>
#include <fstream>
#include <mutex>
#include <random>
#include <string_view>
#include <system_error>
#include <thread>
#include <vector>

#if defined(__i386__) || defined(__x86_64__)
#ifdef _MSC_VER
#include <intrin.h>
#else
#include <x86intrin.h>
#endif
#endif
constexpr const char digit_chars[]{"0123456789"};
constexpr const char hex_chars[]{"0123456789abcdef"};
constexpr const char alpha_chars[]{"abcdefghijklmnopqrstuvwxyz"};
constexpr const char alnum_chars[]{ "abcdefghijklmnopqrstuvwxyz0123456789" };
constexpr const char base64_chars[]{"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"};
constexpr const char base64url_chars[]{"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"};

namespace
{
	//random.org按位计费，范围是2的幂时每个数恰好耗费位数、没有拒绝采样的额外开销，
	//所以统一请求0..65535（每个数16位），取用时再在本地按需切位
	constexpr int kBatch{ 1000 };
	constexpr int kWordBits{ 16 };
	constexpr int kWordMax{ (1 << kWordBits) - 1 };
	constexpr size_t kBatchBits{ static_cast<size_t>(kBatch) * kWordBits };
	//池内剩余位数低于该比例时后台补充
	constexpr int kRefillPercent{ 25 };
	//每次掷骰最多尝试的次数，每次被拒绝的概率不超过1/2，连续失败的概率可忽略
	constexpr int kMaxAttempts{ 64 };
	constexpr int kMaxBackoff{ 900 };

	struct BitPool
	{
		std::mutex mtx;
		std::mutex fileMtx;
		//words[cursor]之后是尚未取用的整字；bitBuf的低bitCnt位是已取出但没用完的零散位
		std::vector<uint16_t> words;
		size_t cursor{ 0 };
		uint64_t bitBuf{ 0 };
		int bitCnt{ 0 };
		std::atomic<int> mode{ 0 };
		std::atomic<unsigned long long> realRolls{ 0 };
		std::atomic<unsigned long long> fallbackRolls{ 0 };
		bool refilling{ false };
		bool stopping{ false };
		//自上次落盘后池内容有变化
		bool dirty{ false };
		//上次补充失败的原因，为空表示成功
		std::string lastError;
		int backoff{ 60 };
		time_t nextAttempt{ 0 };
		size_t fetchedTotal{ 0 };
		std::filesystem::path cacheFile;
		std::thread worker;

		size_t bitsLeft() const { return (words.size() - cursor) * kWordBits + bitCnt; }
		size_t threshold() const { return kBatchBits * kRefillPercent / 100; }
	};

	//刻意不析构：补充线程可能在本文件的静态对象析构之后才收尾
	BitPool& pool()
	{
		static BitPool* instance{ new BitPool };
		return *instance;
	}

	//random.org取数失败时正文以"Error:"开头且带HTTP 503，这里逐行严格校验，
	//任何非纯数字行、越界值或数量不符都视为失败，绝不把脏数据灌进池里
	bool parseBatch(const std::string& body, std::vector<uint16_t>& out)
	{
		out.clear();
		size_t pos{ 0 };
		while (pos < body.size()) {
			size_t end{ body.find('\n', pos) };
			if (end == std::string::npos)end = body.size();
			std::string_view line{ body.data() + pos, end - pos };
			pos = end + 1;
			while (!line.empty() && (line.back() == '\r' || line.back() == ' '))line.remove_suffix(1);
			if (line.empty())continue;
			int value{ 0 };
			auto res{ std::from_chars(line.data(), line.data() + line.size(), value) };
			if (res.ec != std::errc{} || res.ptr != line.data() + line.size() || value < 0 || value > kWordMax)
				return false;
			out.push_back(static_cast<uint16_t>(value));
		}
		return out.size() == static_cast<size_t>(kBatch);
	}

	enum class QuotaState { Ok, NetFail, BadReply };

	//额度查询本身不耗额度
	QuotaState queryQuota(long long& quota)
	{
		std::string body;
		if (!Network::GET("https://www.random.org/quota/?format=plain", body))return QuotaState::NetFail;
		std::string_view text{ body };
		while (!text.empty() && isspace(static_cast<unsigned char>(text.back())))text.remove_suffix(1);
		while (!text.empty() && isspace(static_cast<unsigned char>(text.front())))text.remove_prefix(1);
		auto res{ std::from_chars(text.data(), text.data() + text.size(), quota) };
		if (text.empty() || res.ec != std::errc{} || res.ptr != text.data() + text.size())return QuotaState::BadReply;
		return QuotaState::Ok;
	}

	bool stopRequested()
	{
		BitPool& p{ pool() };
		std::lock_guard lock{ p.mtx };
		return p.stopping;
	}

	bool fetchBatch(std::vector<uint16_t>& out, std::string& err)
	{
		out.clear();
		long long quota{ 0 };
		switch (queryQuota(quota)) {
		case QuotaState::NetFail:
			err = "无法连接random.org";
			return false;
		case QuotaState::Ok:
			if (quota < static_cast<long long>(kBatchBits)) {
				err = "random.org今日额度不足(剩余" + std::to_string(quota) + "位)";
				return false;
			}
			break;
		case QuotaState::BadReply:
			//额度接口异常不该挡住取数，取数结果自己会严格校验
			break;
		}
		if (stopRequested()) {
			err = "已停止";
			return false;
		}
		const std::string url{ "https://www.random.org/integers/?num=" + std::to_string(kBatch)
			+ "&min=0&max=" + std::to_string(kWordMax)
			+ "&col=1&base=10&format=plain&rnd=new" };
		//Network::GET在超时、非200响应或空响应体时都返回false
		std::string body;
		if (!Network::GET(url, body)) {
			err = "取数请求失败";
			return false;
		}
		if (!parseBatch(body, out)) {
			out.clear();
			err = "random.org返回数据无效";
			return false;
		}
		return true;
	}

	void refill()
	{
		BitPool& p{ pool() };
		std::vector<uint16_t> batch;
		std::string err;
		const bool ok{ fetchBatch(batch, err) };
		if (ok) {
			{
				std::lock_guard lock{ p.mtx };
				//已取用的前缀会一直占着数组，攒够了就整体前移一次
				if (p.cursor >= 4096) {
					p.words.erase(p.words.begin(), p.words.begin() + p.cursor);
					p.cursor = 0;
				}
				p.words.insert(p.words.end(), batch.begin(), batch.end());
				p.fetchedTotal += batch.size();
				p.dirty = true;
			}
			RandomGenerator::Save();
		}
		//落盘完成后再解除"补充中"，这样启动下一次补充时的join总能立刻返回
		std::lock_guard lock{ p.mtx };
		p.refilling = false;
		if (ok) {
			p.lastError.clear();
			p.backoff = 60;
			p.nextAttempt = 0;
		}
		else {
			p.lastError = err;
			p.nextAttempt = time(nullptr) + p.backoff;
			if (p.backoff < kMaxBackoff)p.backoff = std::min(p.backoff * 2, kMaxBackoff);
		}
	}

	//调用方须持有p.mtx
	void startRefillLocked(BitPool& p)
	{
		if (p.stopping || p.refilling || p.cacheFile.empty() || p.mode.load() != 1)return;
		if (p.bitsLeft() > p.threshold())return;
		//补充失败后按退避重试，避免离线或额度耗尽时反复打random.org
		if (p.nextAttempt && time(nullptr) < p.nextAttempt)return;
		try {
			if (p.worker.joinable())p.worker.join();
			p.refilling = true;
			p.worker = std::thread{ refill };
		}
		catch (const std::system_error&) {
			p.refilling = false;
		}
	}

	void maybeRefill()
	{
		BitPool& p{ pool() };
		std::lock_guard lock{ p.mtx };
		startRefillLocked(p);
	}

	//取k位(1<=k<=32)。调用方须持有p.mtx，并已确认位数足够
	uint64_t takeBits(BitPool& p, int k)
	{
		while (p.bitCnt < k) {
			p.bitBuf = (p.bitBuf << kWordBits) | p.words[p.cursor++];
			p.bitCnt += kWordBits;
		}
		p.bitCnt -= k;
		const uint64_t value{ p.bitBuf >> p.bitCnt };
		p.bitBuf &= (uint64_t{ 1 } << p.bitCnt) - 1;
		return value;
	}

	//能容纳range个取值所需的位数，即ceil(log2(range))，range>=2
	int bitLength(uint64_t range)
	{
		int k{ 1 };
		while ((uint64_t{ 1 } << k) < range)++k;
		return k;
	}

	//从池中取[0,range)上的均匀整数：取k位，落在range之外就丢弃这k位重取，严格无偏。
	//池里位数不足或连续被拒绝时返回false，由调用方降级；无论成败都顺带检查是否该补充
	bool drawFromPool(uint64_t range, uint64_t& result)
	{
		const int k{ bitLength(range) };
		BitPool& p{ pool() };
		std::lock_guard lock{ p.mtx };
		bool ok{ false };
		for (int attempt{ 0 }; attempt < kMaxAttempts && p.bitsLeft() >= static_cast<size_t>(k); ++attempt) {
			const uint64_t value{ takeBits(p, k) };
			p.dirty = true;
			if (value < range) {
				result = value;
				ok = true;
				break;
			}
		}
		startRefillLocked(p);
		return ok;
	}

	//Keep the engine function-local: a namespace-scope engine would be dynamically
	//initialised in unspecified order relative to other translation units, and the global
	//`console` already calls genKey() from its own dynamic initialiser
	//(authkey_pub/authkey_pri). Reaching this point before the engine is constructed means
	//drawing from a still-zero engine, which returns 0 forever - and the rejection loop in
	//MSVC's uniform_int_distribution never accepts 0, so DllMain spins until the process
	//is killed. Note MSVC defines neither __i386__ nor __x86_64__, so this used to be the
	//global-engine branch on Windows.
	int pseudoRandint(int lowest, int highest)
	{
		static std::mt19937 gen(static_cast<unsigned int>(RandomGenerator::GetCycleCount()));
		//掷骰会来自会话线程、定时任务线程与WebUI线程，引擎与分布对象都不可重入
		static std::mutex engineMtx;
		std::lock_guard lock{ engineMtx };
		std::uniform_int_distribution<int> dis(lowest, highest);
		return dis(gen);
	}

	//读取落盘的池。文件缺失、损坏或内容不合法时返回false且不改动输出
	bool readPool(const std::filesystem::path& file, std::vector<uint16_t>& words, uint64_t& bitBuf, int& bitCnt)
	{
		try {
			std::ifstream fin{ file, std::ios::binary };
			if (!fin)return false;
			const nlohmann::json j{ nlohmann::json::parse(fin) };
			if (j.at("version").get<int>() != 1)return false;
			const int cnt{ j.at("bitCnt").get<int>() };
			const uint64_t buf{ j.at("bitBuf").get<uint64_t>() };
			//取用后零散位总是少于一个整字
			if (cnt < 0 || cnt >= kWordBits || buf >= (uint64_t{ 1 } << cnt))return false;
			std::vector<uint16_t> loaded;
			for (const auto& item : j.at("words")) {
				if (!item.is_number_unsigned() || item.get<uint64_t>() > static_cast<uint64_t>(kWordMax))return false;
				loaded.push_back(item.get<uint16_t>());
			}
			words = std::move(loaded);
			bitBuf = buf;
			bitCnt = cnt;
			return true;
		}
		catch (...) {
			return false;
		}
	}
}

namespace RandomGenerator
{
	unsigned long long GetCycleCount()
	{
#if defined(__i386__) || defined(__x86_64__)
		return __rdtsc();
#else
		return static_cast<unsigned long long> (std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count());
#endif	
	}

	void Configure(int mode)
	{
		pool().mode.store(mode == 1 ? 1 : 0);
		if (pool().mode.load() == 1)maybeRefill();
	}

	void Load(const std::filesystem::path& cacheFile)
	{
		BitPool& p{ pool() };
		std::vector<uint16_t> words;
		uint64_t bitBuf{ 0 };
		int bitCnt{ 0 };
		{
			std::lock_guard fileLock{ p.fileMtx };
			if (!readPool(cacheFile, words, bitBuf, bitCnt)) {
				words.clear();
				bitBuf = 0;
				bitCnt = 0;
			}
		}
		std::lock_guard lock{ p.mtx };
		p.cacheFile = cacheFile;
		p.words = std::move(words);
		p.cursor = 0;
		p.bitBuf = bitBuf;
		p.bitCnt = bitCnt;
		p.dirty = false;
		p.stopping = false;
		p.lastError.clear();
		p.backoff = 60;
		p.nextAttempt = 0;
	}

	void Save()
	{
		BitPool& p{ pool() };
		std::filesystem::path file;
		nlohmann::json j;
		{
			std::lock_guard lock{ p.mtx };
			if (p.cacheFile.empty() || !p.dirty)return;
			file = p.cacheFile;
			j["version"] = 1;
			j["bitBuf"] = p.bitBuf;
			j["bitCnt"] = p.bitCnt;
			j["words"] = std::vector<uint16_t>(p.words.begin() + p.cursor, p.words.end());
			p.dirty = false;
		}
		//先写临时文件再改名，崩溃时旧文件不会被写坏
		std::lock_guard fileLock{ p.fileMtx };
		std::error_code ec;
		std::filesystem::create_directories(file.parent_path(), ec);
		std::filesystem::path tmp{ file };
		tmp += ".tmp";
		bool ok{ false };
		{
			std::ofstream fout{ tmp, std::ios::binary | std::ios::trunc };
			if (fout) {
				fout << j.dump();
				fout.flush();
				ok = static_cast<bool>(fout);
			}
		}
		if (ok) {
			std::filesystem::rename(tmp, file, ec);
			ok = !ec;
		}
		if (!ok) {
			std::filesystem::remove(tmp, ec);
			std::lock_guard lock{ p.mtx };
			p.dirty = true;
		}
	}

	void Shutdown()
	{
		BitPool& p{ pool() };
		std::thread worker;
		{
			std::lock_guard lock{ p.mtx };
			p.stopping = true;
			worker = std::move(p.worker);
		}
		if (worker.joinable())worker.join();
	}

	std::string ModeText()
	{
		return pool().mode.load() == 1 ? "真随机(random.org)" : "伪随机(默认)";
	}

	std::string Report()
	{
		BitPool& p{ pool() };
		std::lock_guard lock{ p.mtx };
		const bool real{ p.mode.load() == 1 };
		const size_t bits{ p.bitsLeft() };
		std::string res{ "随机数模式：" + ModeText() };
		res += "\n随机数池剩余：" + std::to_string(bits) + "位";
		//掷一次1..N骰子平均耗费k*2^k/N位，k=ceil(log2 N)，换算成常用骰子的可掷次数
		std::string approx;
		for (int faces : { 6, 10, 20, 100 }) {
			const int k{ bitLength(static_cast<uint64_t>(faces)) };
			const double perRoll{ static_cast<double>(k) * static_cast<double>(uint64_t{ 1 } << k) / faces };
			if (!approx.empty())approx += " / ";
			approx += "d" + std::to_string(faces) + "×" + std::to_string(static_cast<unsigned long long>(static_cast<double>(bits) / perRoll));
		}
		res += "（约可掷 " + approx + "）";
		res += "\n补充状态：";
		if (p.refilling)res += "补充中";
		else if (!p.lastError.empty()) {
			res += "上次取数失败，" + p.lastError;
			const time_t now{ time(nullptr) };
			if (real && p.nextAttempt > now)res += "，约" + std::to_string(static_cast<long long>(p.nextAttempt - now)) + "秒后重试";
		}
		else res += real ? "空闲" : "未启用(伪随机模式不补充)";
		res += "\n本次运行：真随机" + std::to_string(p.realRolls.load()) + "次 / 降级为伪随机" + std::to_string(p.fallbackRolls.load()) + "次";
		return res;
	}

	int Randint(int lowest, int highest)
	{
		if (lowest >= highest)return lowest;
		BitPool& p{ pool() };
		//真随机模式下池里位数够就用真随机数；池空、退避中或取数失败时当次降级为伪随机，
		//掷骰因此永不会被网络阻塞
		if (p.mode.load() == 1) {
			const uint64_t range{ static_cast<uint64_t>(static_cast<int64_t>(highest) - static_cast<int64_t>(lowest)) + 1 };
			uint64_t value{ 0 };
			if (drawFromPool(range, value)) {
				++p.realRolls;
				return static_cast<int>(static_cast<int64_t>(lowest) + static_cast<int64_t>(value));
			}
			++p.fallbackRolls;
		}
		return pseudoRandint(lowest, highest);
	}
	std::string genKey(size_t len, Code mode) {
		std::string res;
		std::string charset;
		switch (mode) {
		case RandomGenerator::Code::Hex:
			charset = hex_chars;
			break;
		case RandomGenerator::Code::Alpha:
			charset = alpha_chars;
			break;
		case RandomGenerator::Code::Alnum:
			charset = alnum_chars;
			break;
		case RandomGenerator::Code::Base64:
			charset = base64_chars;
			break;
		case RandomGenerator::Code::UrlBase64:
			charset = base64url_chars;
			break;
		case RandomGenerator::Code::Decimal:
		default:
			charset = digit_chars;
			break;
		}
		size_t size{ charset.length() - 1 };
		//认证码不走真随机池：random.org会知道它发出的每个数，认证码不该有第三方知情
		for (size_t i = 0; i < len; ++i) {
			res += charset[pseudoRandint(0, static_cast<int>(size))];
		}
		return res;
	}
}
