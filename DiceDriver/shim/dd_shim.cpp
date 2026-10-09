// DiceDriver native shim (x64, MSVC, /MT).
//
// Dice's host API is C++ ABI: std::string / std::set references, and an unordered_map<string, void*>
// table handed over through eventStartUp. Python cannot produce those, so this DLL owns the table
// and the 39 thunks with the exact signatures from QQAPI/DDAPI.cpp. Every thunk packs its arguments
// into plain C types and forwards to ONE Python callback (DispatchFn):
//
//     long long dispatch(int api, const long long* ints, int nints, const char* str, char* out, int outCap)
//
//   * ints/str : input arguments (integers and bool in order; the single string argument, UTF-8)
//   * out      : output buffer; string results are written as UTF-8, set results as "1,2,3"
//   * return   : bool/int/long long result, or GroupSize packed as (curr << 32) | max
//
// The 6 interfaces the project decided not to implement are intentionally NOT registered, so Dice
// falls back to its documented defaults (see docs/DiceDriver-interface-duties.md section 0).
//
// Build with the same MSVC toolset and /MT as the Dice DLL (see build-shim.ps1).

#include <windows.h>

#include <cstdlib>
#include <initializer_list>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>

namespace {

struct GroupSize_t {
    unsigned int currSize = 0;
    unsigned int maxSize = 0;
};

using api_list = std::unordered_map<std::string, void*>;
using DispatchFn = long long (*)(int, const long long*, int, const char*, char*, int);

constexpr int kOutCap = 1 << 20;  // 1 MiB per calling thread, allocated lazily

DispatchFn g_dispatch = nullptr;

enum Api : int {
    API_DriverVer,
    API_GetRootDir,
    API_Reload,
    API_Remake,
    API_Killme,
    API_DebugLog,
    API_DebugMsg,
    API_DiceHeartbeat,
    API_DiceUpdate,
    API_GetNick,
    API_GetTinyID,
    API_SendPrivateMsg,
    API_SendGroupMsg,
    API_IsFriend,
    API_GetFriendQQList,
    API_GetGroupIDList,
    API_GetGroupMemberList,
    API_GetGroupAdminList,
    API_GetGroupAuth,
    API_IsGroupAdmin,
    API_IsGroupOwner,
    API_IsGroupMember,
    API_AnswerFriendRequest,
    API_AnswerGroupInvited,
    API_GetGroupSize,
    API_GetGroupName,
    API_GetGroupNick,
    API_GetGroupLastMsg,
    API_PrintGroupInfo,
    API_SetGroupKick,
    API_SetGroupBan,
    API_SetGroupAdmin,
    API_SetGroupCard,
    API_SetGroupTitle,
    API_SetGroupWholeBan,
    API_SetGroupLeave,
    API_SetDiscussLeave,
    API_UploadGroupFile,
    API_SendPrivateFile,
    API_COUNT
};

// Names are the keys Dice looks up in the table; "_DriverVer" keeps its leading underscore.
const char* const kApiNames[API_COUNT] = {
    "_DriverVer", "GetRootDir", "Reload", "Remake", "Killme", "DebugLog", "DebugMsg", "DiceHeartbeat",
    "DiceUpdate", "GetNick", "GetTinyID", "SendPrivateMsg", "SendGroupMsg", "IsFriend", "GetFriendQQList",
    "GetGroupIDList", "GetGroupMemberList", "GetGroupAdminList", "GetGroupAuth", "IsGroupAdmin",
    "IsGroupOwner", "IsGroupMember", "AnswerFriendRequest", "AnswerGroupInvited", "GetGroupSize",
    "GetGroupName", "GetGroupNick", "GetGroupLastMsg", "PrintGroupInfo", "SetGroupKick", "SetGroupBan",
    "SetGroupAdmin", "SetGroupCard", "SetGroupTitle", "SetGroupWholeBan", "SetGroupLeave",
    "SetDiscussLeave", "UploadGroupFile", "SendPrivateFile",
};

char* outBuffer() {
    thread_local std::unique_ptr<char[]> buf(new char[kOutCap]);
    return buf.get();
}

struct Result {
    long long ret;
    const char* out;
};

Result invoke(int api, std::initializer_list<long long> ints, const char* str = "") {
    char* out = outBuffer();
    out[0] = 0;
    if (!g_dispatch) return {0, out};
    long long r = g_dispatch(api, ints.begin(), static_cast<int>(ints.size()), str, out, kOutCap);
    out[kOutCap - 1] = 0;
    return {r, out};
}

std::set<long long> parseIdSet(const char* s) {
    std::set<long long> ids;
    while (*s) {
        char* end = nullptr;
        long long v = std::strtoll(s, &end, 10);
        if (end == s) break;
        ids.insert(v);
        s = end;
        if (*s == ',') ++s;
    }
    return ids;
}

// ---------------------------------------------------------------- thunks (signatures: QQAPI/DDAPI.cpp)

const char* T_DriverVer() {
    static std::string ver;
    static std::once_flag once;
    std::call_once(once, [] { ver = invoke(API_DriverVer, {}).out; });
    return ver.c_str();
}

const std::string& T_GetRootDir() {
    thread_local std::string s;
    s = invoke(API_GetRootDir, {}).out;
    return s;
}

bool T_Reload() { return invoke(API_Reload, {}).ret != 0; }
bool T_Remake() { return invoke(API_Remake, {}).ret != 0; }
void T_Killme() { invoke(API_Killme, {}); }
void T_DebugLog(const std::string& s) { invoke(API_DebugLog, {}, s.c_str()); }
void T_DebugMsg(long long login, const std::string& s) { invoke(API_DebugMsg, {login}, s.c_str()); }
void T_DiceHeartbeat(long long login, const std::string& s) { invoke(API_DiceHeartbeat, {login}, s.c_str()); }

bool T_DiceUpdate(const std::string& ver, std::string& ret) {
    Result r = invoke(API_DiceUpdate, {}, ver.c_str());
    ret = r.out;
    return r.ret != 0;
}

const std::string& T_GetNick(long long login, long long qq) {
    thread_local std::string s;
    s = invoke(API_GetNick, {login, qq}).out;
    return s;
}

long long T_GetTinyID(long long login) { return invoke(API_GetTinyID, {login}).ret; }

void T_SendPrivateMsg(long long login, long long to, const std::string& m) {
    invoke(API_SendPrivateMsg, {login, to}, m.c_str());
}
void T_SendGroupMsg(long long login, long long to, const std::string& m) {
    invoke(API_SendGroupMsg, {login, to}, m.c_str());
}

bool T_IsFriend(long long login, long long qq, bool def) {
    return invoke(API_IsFriend, {login, qq, def ? 1 : 0}).ret != 0;
}

const std::set<long long>& T_GetFriendQQList(long long login) {
    thread_local std::set<long long> s;
    s = parseIdSet(invoke(API_GetFriendQQList, {login}).out);
    return s;
}
const std::set<long long>& T_GetGroupIDList(long long login) {
    thread_local std::set<long long> s;
    s = parseIdSet(invoke(API_GetGroupIDList, {login}).out);
    return s;
}
const std::set<long long>& T_GetGroupMemberList(long long login, long long gid) {
    thread_local std::set<long long> s;
    s = parseIdSet(invoke(API_GetGroupMemberList, {login, gid}).out);
    return s;
}
const std::set<long long>& T_GetGroupAdminList(long long login, long long gid) {
    thread_local std::set<long long> s;
    s = parseIdSet(invoke(API_GetGroupAdminList, {login, gid}).out);
    return s;
}

int T_GetGroupAuth(long long login, long long gid, long long qq) {
    return static_cast<int>(invoke(API_GetGroupAuth, {login, gid, qq}).ret);
}
bool T_IsGroupAdmin(long long login, long long gid, long long qq, bool def) {
    return invoke(API_IsGroupAdmin, {login, gid, qq, def ? 1 : 0}).ret != 0;
}
bool T_IsGroupOwner(long long login, long long gid, long long qq, bool def) {
    return invoke(API_IsGroupOwner, {login, gid, qq, def ? 1 : 0}).ret != 0;
}
bool T_IsGroupMember(long long login, long long gid, long long qq, bool def) {
    return invoke(API_IsGroupMember, {login, gid, qq, def ? 1 : 0}).ret != 0;
}

void T_AnswerFriendRequest(long long login, long long qq, int resp, const std::string& msg) {
    invoke(API_AnswerFriendRequest, {login, qq, resp}, msg.c_str());
}
void T_AnswerGroupInvited(long long login, long long gid, int resp) {
    invoke(API_AnswerGroupInvited, {login, gid, resp});
}

GroupSize_t T_GetGroupSize(long long login, long long gid) {
    unsigned long long packed = static_cast<unsigned long long>(invoke(API_GetGroupSize, {login, gid}).ret);
    GroupSize_t gs;
    gs.currSize = static_cast<unsigned int>(packed >> 32);
    gs.maxSize = static_cast<unsigned int>(packed & 0xFFFFFFFFull);
    return gs;
}

const std::string& T_GetGroupName(long long login, long long gid) {
    thread_local std::string s;
    s = invoke(API_GetGroupName, {login, gid}).out;
    return s;
}
const std::string& T_GetGroupNick(long long login, long long gid, long long qq) {
    thread_local std::string s;
    s = invoke(API_GetGroupNick, {login, gid, qq}).out;
    return s;
}
long long T_GetGroupLastMsg(long long login, long long gid, long long qq) {
    return invoke(API_GetGroupLastMsg, {login, gid, qq}).ret;
}
const std::string& T_PrintGroupInfo(long long login, long long gid) {
    thread_local std::string s;
    s = invoke(API_PrintGroupInfo, {login, gid}).out;
    return s;
}

void T_SetGroupKick(long long login, long long gid, long long qq) { invoke(API_SetGroupKick, {login, gid, qq}); }
void T_SetGroupBan(long long login, long long gid, long long qq, int sec) {
    invoke(API_SetGroupBan, {login, gid, qq, sec});
}
void T_SetGroupAdmin(long long login, long long gid, long long qq, bool set) {
    invoke(API_SetGroupAdmin, {login, gid, qq, set ? 1 : 0});
}
void T_SetGroupCard(long long login, long long gid, long long qq, const std::string& s) {
    invoke(API_SetGroupCard, {login, gid, qq}, s.c_str());
}
void T_SetGroupTitle(long long login, long long gid, long long qq, const std::string& s) {
    invoke(API_SetGroupTitle, {login, gid, qq}, s.c_str());
}
void T_SetGroupWholeBan(long long login, long long gid, int sec) { invoke(API_SetGroupWholeBan, {login, gid, sec}); }
void T_SetGroupLeave(long long login, long long gid) { invoke(API_SetGroupLeave, {login, gid}); }
void T_SetDiscussLeave(long long login, long long gid) { invoke(API_SetDiscussLeave, {login, gid}); }
bool T_UploadGroupFile(long long login, long long gid, const std::string& p) {
    return invoke(API_UploadGroupFile, {login, gid}, p.c_str()).ret != 0;
}
bool T_SendPrivateFile(long long login, long long qq, const std::string& p) {
    return invoke(API_SendPrivateFile, {login, qq}, p.c_str()).ret != 0;
}

#define THUNK(name) reinterpret_cast<void*>(&name)
void* const kThunks[API_COUNT] = {
    THUNK(T_DriverVer),         THUNK(T_GetRootDir),        THUNK(T_Reload),
    THUNK(T_Remake),            THUNK(T_Killme),            THUNK(T_DebugLog),
    THUNK(T_DebugMsg),          THUNK(T_DiceHeartbeat),     THUNK(T_DiceUpdate),
    THUNK(T_GetNick),           THUNK(T_GetTinyID),         THUNK(T_SendPrivateMsg),
    THUNK(T_SendGroupMsg),      THUNK(T_IsFriend),          THUNK(T_GetFriendQQList),
    THUNK(T_GetGroupIDList),    THUNK(T_GetGroupMemberList), THUNK(T_GetGroupAdminList),
    THUNK(T_GetGroupAuth),      THUNK(T_IsGroupAdmin),      THUNK(T_IsGroupOwner),
    THUNK(T_IsGroupMember),     THUNK(T_AnswerFriendRequest), THUNK(T_AnswerGroupInvited),
    THUNK(T_GetGroupSize),      THUNK(T_GetGroupName),      THUNK(T_GetGroupNick),
    THUNK(T_GetGroupLastMsg),   THUNK(T_PrintGroupInfo),    THUNK(T_SetGroupKick),
    THUNK(T_SetGroupBan),       THUNK(T_SetGroupAdmin),     THUNK(T_SetGroupCard),
    THUNK(T_SetGroupTitle),     THUNK(T_SetGroupWholeBan),  THUNK(T_SetGroupLeave),
    THUNK(T_SetDiscussLeave),   THUNK(T_UploadGroupFile),   THUNK(T_SendPrivateFile),
};
#undef THUNK

// eventStartUp receives a function returning `const api_list&`; Dice copies the table immediately.
const api_list& getApiList() {
    static api_list table;
    static std::once_flag once;
    std::call_once(once, [] {
        for (int i = 0; i < API_COUNT; ++i) table[kApiNames[i]] = kThunks[i];
    });
    return table;
}

}  // namespace

extern "C" {

__declspec(dllexport) void dd_set_dispatch(DispatchFn fn) { g_dispatch = fn; }

// Pass the returned pointer to eventStartUp(initApi, botQQ).
__declspec(dllexport) void* dd_api_list_getter() { return reinterpret_cast<void*>(&getApiList); }

__declspec(dllexport) int dd_api_count() { return API_COUNT; }
__declspec(dllexport) const char* dd_api_name(int id) { return (id >= 0 && id < API_COUNT) ? kApiNames[id] : nullptr; }
__declspec(dllexport) int dd_out_capacity() { return kOutCap; }

}
