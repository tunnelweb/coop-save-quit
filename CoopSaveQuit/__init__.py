# co-op save quit
#
# a save quit that does not kick whoever youre playing with.
#
# open some chests, hit the key, everything comes back fresh and nobody gets kicked out of your
# game. thats the whole thing.
#
# WHY THIS IS NOT JUST A SAVE QUIT
# save quit is what everyone uses to farm, and it works because it throws the session away and
# reloads your character off disk. that also drops anyone playing with you. every other mod that
# claims to do this calls PC.ReturnToTitleScreen, quickload and commander both, and that IS the
# save quit, so they all kick your partner too. theres no flag on it to keep them.
#
# so this does not quit. it uses the games own travel, the same one a fast travel station uses,
# which moves the host AND everyone connected without ending the session:
#
#   game.TravelToStation(station, bForceLevelLoad=True)
#
# host only, and thats fine, travel is server driven so your partner does not need this installed.
# they just come along with you.
#
# THE BIT THAT ACTUALLY MAKES THINGS RESET
# the travel alone does nothing, we tested it, chests stay open. thats not a bug in the travel,
# its that chests are not self resetting objects at all. theyre handed out by the population
# system, and whether a level load makes a spawner forget what it already gave you is one bool,
# bTotalResetOnLevelLoad, and it ships off. plain fast travel doesnt reset chests for the exact
# same reason.
#
# so we flip it on, then travel. thats it. the community has shipped this for years as a text mod
# called Respawning Loot, one set command, we just do it from here so its all one mod.
#
# chests and enemies are separate switches because theyre separate classes. PopulationDefinition
# is chests, vendors and props. WillowPopulationDefinition is enemies, and writing the base class
# does NOT reach it, subclass defaults are built separately. dice chests stack their loot instead
# of rerolling so theyre always carved back out.
#
# things that do NOT work, dont bother retrying them:
#   servertravel                 hard crashes the game, it skips everything BL2 does around a
#                                transition and pulls the map out from under it
#   Reset() / SetInitialState()  run clean, do nothing, these objects have no state machine
#   find_all(..., exact=False)   drags in archetypes living inside asset packages, calling
#                                anything on one reads freed memory and kills the game instantly

from __future__ import annotations

assert __import__("mods_base").__version_info__ >= (1, 12), "Co-op Save Quit needs a newer SDK, please update"

from typing import Any

from mods_base import (
    BoolOption,
    CoopSupport,
    Game,
    ModType,
    SliderOption,
    build_mod,
    get_pc,
    hook,
    keybind,
)
from ui_utils import show_hud_message
from unrealsdk import find_all, find_object, logging
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

TITLE = "Co-op Save Quit"

# UE3 netmode, 3 is client. anything else means were the host and the travel is ours to make.
NM_CLIENT = 3

# Dice chests keep whatever is already inside them instead of rerolling, so they just pile up and
# get annoying to loot. the community mod carves them out and so do we.
_DICE_CHEST = "GD_Aster_Lootables.Balance.PopDef_DiceChest"

reset_lootables = BoolOption(
    "Respawn Chests & Vendors",
    True,
    description=(
        "Chests come back unopened. Vendors count as population too so their item of the day"
        " rerolls on every reload."
    ),
)

reset_enemies = BoolOption(
    "Respawn Enemies",
    True,
    description=(
        "Enemies are their own population class so they need a separate switch. Turn it off if"
        " respawning mission NPCs or allies gets in the way."
    ),
)

restore_position = BoolOption(
    "Restore Position",
    True,
    description="Put you back where you were standing instead of at the stations spawn point.",
)

save_first = BoolOption(
    "Save Before Reloading",
    True,
    description="Save your character before travelling. Leave this on.",
)

# how long to keep shoving you back into place after the load. one shot didnt stick, twice, so
# now we just keep re-applying for a bit, because the game is also moving you during this window
# and whoever writes last wins. bump this if you still end up at the station.
restore_window = SliderOption(
    "Position Restore Window",
    150,
    30,
    600,
    10,
    True,
    description="Frames spent holding you at your old spot after a reload. Raise it if it fails.",
)

# where WE were stood. still kept on its own because the 'have we been moved yet' check below
# needs a single point to measure against.
_saved_location: Any = None
_saved_rotation: Any = None

# where EVERYONE was stood, keyed by player name, so your partner gets put back at her own
# spot and not yours. player names survive the travel, pawns do not.
_saved_positions: dict = {}

# waiting for the new map to come up after a travel
_pending_reload = False
# ticks counted since the travel started. only advances while the game is actually running, which
# is useful in itself, it means loading time does not count against us.
_frames_since_travel = 0
# frames left in the hold window
_hold_frames = 0
_hold_logged = False
_tick_alive_logged = False



def _arm_population_reset() -> tuple[int, int]:
    # these are definition assets, not things placed in the world, and this is a plain bool write
    # with no unrealscript call in it. none of the crash rules apply here.
    #
    # exact=True on purpose, it keeps the archetype junk out.
    lootables = 0
    if reset_lootables.value:
        for pop_def in find_all("PopulationDefinition", exact=True):
            try:
                pop_def.bTotalResetOnLevelLoad = True
                lootables += 1
            except Exception:  # a stale entry just gets skipped, thats fine
                continue

        # and the class default, so anything streamed in later comes up already armed. ue3 only
        # stores what an asset actually overrides so most of them inherit this.
        try:
            find_object(
                "PopulationDefinition", "GearboxFramework.Default__PopulationDefinition"
            ).bTotalResetOnLevelLoad = True
        except Exception:
            logging.warning(f"[{TITLE}] could not arm the population default")

    enemies = 0
    if reset_enemies.value:
        for pop_def in find_all("WillowPopulationDefinition", exact=True):
            try:
                pop_def.bTotalResetOnLevelLoad = True
                enemies += 1
            except Exception:
                continue

    # carve the dice chests back out, after the blanket pass so it actually sticks
    try:
        find_object("PopulationDefinition", _DICE_CHEST).bTotalResetOnLevelLoad = False
    except Exception:
        pass  # not loaded in this map, nothing to carve

    return lootables, enemies


def _players(pc: UObject) -> list:
    """everyone in the session, host and clients.

    GRI.PRIArray is a plain engine thing, no other mod needed. credit to commander for
    showing this is the route that works, but nothing here imports it.
    """
    try:
        gri = pc.WorldInfo.GRI
        if gri is None:
            return []
        return list(gri.PRIArray)
    except Exception:
        return []


def _capture_positions(pc: UObject) -> dict:
    """note down where everyone is stood, keyed by player name."""
    out: dict = {}
    for pri in _players(pc):
        try:
            owner = pri.Owner
            if owner is None or owner.Pawn is None:
                continue
            loc = owner.Pawn.Location
            rot = owner.Rotation
            out[str(pri.PlayerName)] = (loc.X, loc.Y, loc.Z, rot.Pitch, rot.Roll, rot.Yaw)
        except Exception:
            continue
    return out


def _apply_positions(pc: UObject, include_remote: bool) -> str | None:
    # put people back where they were, hands back where we ended up for the log.
    #

    # include_remote is there because you and your partner need totally different treatment.
    #

    # we hold our own position for a whole window of frames, because the game keeps trying to
    # drag us to the station and whoever writes last wins. thats free, its our own client.
    #

    # doing that to a remote player is what made her snap. every one of those writes is a server
    # correction landing on a client thats busy predicting its own movement, so 150 frames of it
    # is 150 corrections, which looks exactly like rubber banding. she gets one at the start and
    # one at the end, nothing in between.
    if not _saved_positions:
        return None

    my_name = None
    try:
        my_name = str(pc.PlayerReplicationInfo.PlayerName)
    except Exception:
        pass

    for pri in _players(pc):
        try:
            owner = pri.Owner
            if owner is None or owner.Pawn is None:
                continue

            name = str(pri.PlayerName)
            is_me = my_name is not None and name == my_name
            if not is_me and not include_remote:
                continue

            saved = _saved_positions.get(name)
            if saved is None:
                continue

            # fresh struct off the CURRENT pawn, the old ones pawn is long gone
            loc = owner.Pawn.Location
            loc.X, loc.Y, loc.Z = saved[0], saved[1], saved[2]
            rot = owner.Rotation
            rot.Pitch, rot.Roll, rot.Yaw = saved[3], saved[4], saved[5]

            if is_me:
                owner.NoFailSetPawnLocation(owner.Pawn, loc)
                owner.ClientSetRotation(rot)
                continue

            # remote player. ClientSetLocation is the engines own "you are here, stop predicting"
            # rpc, which is the thing that avoids the snap. fall back to the server side move if
            # this build does not have it.
            try:
                owner.ClientSetLocation(loc, rot)
            except Exception:
                owner.NoFailSetPawnLocation(owner.Pawn, loc)
                owner.ClientSetRotation(rot)
        except Exception:
            continue

    if pc.Pawn is None:
        return None
    now = pc.Pawn.Location
    return f"({now.X:.0f}, {now.Y:.0f}, {now.Z:.0f})"


@keybind(
    "Co-op Save Quit",
    key="F6",
    description="Reload the map, fresh loot, nobody gets dropped from your game.",
)
def lobby_safe_reload() -> None:
    global _saved_location, _saved_rotation

    pc = get_pc()
    world_info = pc.WorldInfo

    if world_info.NetMode == NM_CLIENT:
        show_hud_message(TITLE, "Host only, someone else is hosting this game.")
        return

    game = world_info.Game
    if game is None:
        show_hud_message(TITLE, "No game info, cannot travel.")
        return

    # the station we last saved at. usually the one in the map youre stood in, because BL2 saves
    # at new-u points and map entrances as well as fast travel ones.
    try:
        station = pc.GetSavedTravelStation(pc.GetCachedSaveGame())
    except Exception as e:
        logging.error(f"[{TITLE}] could not find a station: {e!r}")
        station = None

    if station is None:
        show_hud_message(TITLE, "No travel station for this map, cannot reload here.")
        return

    # arm it BEFORE we travel, the flag is read on the way in
    lootables, enemies = _arm_population_reset()
    logging.info(f"[{TITLE}] armed {lootables} lootable + {enemies} enemy population defs")

    _saved_location = None
    _saved_rotation = None
    if restore_position.value:
        if pc.Pawn is None:
            logging.warning(f"[{TITLE}] no pawn, cannot remember where you were")
        else:
            # copy the NUMBERS out, not the structs. these point at the pawns own memory and the
            # pawn does not survive a travel.
            loc = pc.Pawn.Location
            rot = pc.Rotation
            _saved_location = (loc.X, loc.Y, loc.Z)
            _saved_rotation = (rot.Pitch, rot.Roll, rot.Yaw)

            global _saved_positions
            _saved_positions = _capture_positions(pc)
            logging.info(
                f"[{TITLE}] remembered {len(_saved_positions)} player(s),"
                f" mine ({loc.X:.0f}, {loc.Y:.0f}, {loc.Z:.0f})"
            )

    if save_first.value:
        try:
            pc.SaveGame()
        except Exception as e:
            logging.error(f"[{TITLE}] SaveGame() failed: {e!r}")
            show_hud_message(TITLE, "Couldnt save, so im not travelling.")
            return

    global _pending_reload, _frames_since_travel, _hold_frames
    _pending_reload = True
    _frames_since_travel = 0
    _hold_frames = 0

    logging.info(f"[{TITLE}] travelling to {station.Name}")
    game.TravelToStation(station, True)


# theres deliberately no WillowClientDisableLoadingMovie hook any more. it never fired
# once, across every travel, so the position restore hanging off it never ran. the tick below
# works out for itself when the new map is up.


# NOTE ON THE COLON. modern sdk hooks are "Package.Class:Function", with a COLON before the
# function. the legacy mods all use dots and i copied that, so NONE of my hooks ever fired, not
# this one and not the load one before it. every mods_base and ui_utils hook uses the colon form.
@hook("WillowGame.WillowGameViewportClient:Tick")
def _on_tick(
    _obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    # per frame, so bail instantly when theres nothing on. AsyncUtil hooks this same function so
    # the path is real, and the one time log below proves its actually running.
    global _pending_reload, _frames_since_travel
    global _hold_frames, _hold_logged, _tick_alive_logged

    if not _tick_alive_logged:
        _tick_alive_logged = True
        logging.info(f"[{TITLE}] tick hook alive")

    if not _pending_reload and _hold_frames <= 0:
        return

    pc = get_pc(possibly_loading=True)
    if pc is None:
        return

    if _pending_reload:
        if pc.Pawn is None:
            return

        _frames_since_travel += 1

        # dont wait to SEE the pawn disappear, we never will. the viewport does not tick while the
        # game is loading, so that moment happens with the hook asleep and we sat there forever
        # waiting for it.
        #
        # instead notice that youve been MOVED. after the travel the game drops you at the station,
        # which is a long way from where you pressed the key. that IS the signal, and its exactly
        # the condition we want to correct anyway.
        moved = False
        if _saved_location is None:
            # nothing to compare against, just give it a moment and re-arm
            moved = _frames_since_travel > 120
        else:
            try:
                here = pc.Pawn.Location
                dx = here.X - _saved_location[0]
                dy = here.Y - _saved_location[1]
                dz = here.Z - _saved_location[2]
                moved = (dx * dx + dy * dy + dz * dz) > (1000.0 * 1000.0)
            except Exception:
                return

        # belt and braces, if the station happens to be right where you stood wed never see a
        # big enough jump, so give up waiting eventually and just carry on
        if not moved and _frames_since_travel < 900:
            return

        _pending_reload = False
        logging.info(
            f"[{TITLE}] new map up after {_frames_since_travel} frames (moved={moved})"
        )

        # re-arm here, this map streamed in its own population defs
        _arm_population_reset()

        if _saved_location is None:
            show_hud_message(TITLE, "Map reloaded.")
            return

        _hold_frames = int(restore_window.value)
        _hold_logged = False
        show_hud_message(TITLE, "Map reloaded, lobby intact.")

    if _hold_frames > 0:
        _hold_frames -= 1
        # only bother her on the way in and on the way out, never in between
        touch_remote = not _hold_logged or _hold_frames == 0
        where = _apply_positions(pc, touch_remote)
        if not _hold_logged:
            _hold_logged = True
            logging.info(f"[{TITLE}] holding position, first apply landed at {where}")
        elif _hold_frames == 0:
            logging.info(f"[{TITLE}] hold finished, final position {where}")


def _on_enable() -> None:
    # arm straight away so it also covers a normal fast travel or walking between maps, not just
    # our own key
    lootables, enemies = _arm_population_reset()
    logging.info(f"[{TITLE}] enabled, armed {lootables} lootable + {enemies} enemy population defs")


build_mod(
    name=TITLE,
    author="web",
    version="1.6",
    description=(
        "Reload the map for fresh loot without kicking your co-op partner. Uses the game's own"
        " travel instead of a save quit, and arms the population reset flag so chests, vendors and"
        " enemies actually refresh. Host only - your partner does not need this installed."
    ),
    mod_type=ModType.Standard,
    supported_games=Game.BL2 | Game.TPS | Game.AoDK,
    coop_support=CoopSupport.HostOnly,
    on_enable=_on_enable,
)
