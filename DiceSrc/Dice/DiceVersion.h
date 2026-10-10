/*- encoding: ascii -*/
/*
@File     :   DiceVersion.h
@Desc     :   Single source of truth for the Dice! version number: edit the macros below and
              rebuild (build-dicesrc.bat -BuildOnly). The .bot/.help display string, the HTTP
              User-Agent and the DLL file properties all follow. See DiceSrc\docs section 2.8.
@Note     :   Keep this file ASCII only, with no includes and no C++ types: Resource.rc includes
              it as well, and rc.exe reads it under code page 936, where UTF-8 CJK bytes swallow
              the following newline and abort the build (fatal error RC1004 / RC2104). The file
              must also end with a newline, for the same reason.
*/
#pragma once

// Semantic version. Bump these for a release; TAG is "p"/"beta1"/... or "" for a plain release.
#define DICE_VERSION_MAJOR 1
#define DICE_VERSION_MINOR 2
#define DICE_VERSION_PATCH 0
#define DICE_VERSION_TAG   "p"

// Build number, formerly Dice_Build. The cloud heartbeat and module installs compare against it:
// raising it makes .update believe this copy is newer than the cloud, lowering it does the opposite.
#define DICE_BUILD_NUMBER  668

#define DICE_STR_(x) #x
#define DICE_STR(x) DICE_STR_(x)
// Compile-time concatenation of the values above, e.g. 1.0.0p; never copy a literal elsewhere.
#define DICE_VERSION_TEXT DICE_STR(DICE_VERSION_MAJOR) "." DICE_STR(DICE_VERSION_MINOR) "." DICE_STR(DICE_VERSION_PATCH) DICE_VERSION_TAG
