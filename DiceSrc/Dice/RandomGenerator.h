/*
 *  _______     ________    ________    ________    __
 * |   __  \   |__    __|  |   _____|  |   _____|  |  |
 * |  |  |  |     |  |     |  |        |  |_____   |  |
 * |  |  |  |     |  |     |  |        |   _____|  |__|
 * |  |__|  |   __|  |__   |  |_____   |  |_____    __
 * |_______/   |________|  |________|  |________|  |__|
 *
 * Dice! QQ Dice Robot for TRPG
 * Copyright (C) 2018-2021 w4123溯洄
 * Copyright (C) 2019-2022 String.Empty
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
#pragma once
#ifndef DICE_RANDOM_GENERATOR
#define DICE_RANDOM_GENERATOR
#include <filesystem>
#include <string>

namespace RandomGenerator {
	unsigned long long GetCycleCount();
	int Randint(int lowest, int highest);

	//随机数模式：0-伪随机（默认）；1-真随机（random.org取数缓存，池空或取数失败自动降级）
	void Configure(int mode);
	//载入本地真随机数池，须在DiceDir就绪后调用
	void Load(const std::filesystem::path& cacheFile);
	//有变化时保存池，没有变化则什么都不做
	void Save();
	//结束流程：等待补充线程收尾，避免进程卸载时仍有网络请求在途
	void Shutdown();
	//当前模式的一行描述，供.admin state展示
	std::string ModeText();
	//模式、池剩余位数、补充状态与降级次数的多行说明，供.admin randstate展示
	std::string Report();

	enum class Code { Decimal, Hex, Alpha, Alnum, Base64, UrlBase64};
	std::string genKey(size_t len, Code = Code::Decimal);
}
#endif /*DICE_RANDOM_GENERATOR*/
