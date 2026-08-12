# co-op save quit
#
# save quitting is how you farm, and it kicks everyone else out of your game. every other mod
# that does this calls PC.ReturnToTitleScreen, which IS the save quit, so they all kick too.
# this one flips bTotalResetOnLevelLoad on the population defs and then travels with the games
# own fast travel, so the session survives and chests, vendors and enemies come back fresh.
# chests and enemies are separate classes, writing the base class does NOT reach the subclass.
#
# read only cant be done for a partner from here, saving happens on their machine, so the client
# copy of this mod watches for the host reloading under it and rolls its own missions back.
#
# dead ends, dont retry these:
#   servertravel hard crashes, it skips everything bl2 does around a transition
#   Reset() / SetInitialState() run clean and do nothing
#   find_all exact=False drags in archetypes, calling anything on one reads freed memory and
#   kills the game, property reads and writes are fine
#   ClientExpectedResourcePools sits empty on the host, pools come off the pawn or a class sweep

from __future__ import annotations

assert __import__("mods_base").__version_info__ >= (1, 12), "Co-op Save Quit needs a newer SDK, please update"

import time
from typing import Any

from mods_base import (
    BoolOption,
    CoopSupport,
    Game,
    ModType,
    build_mod,
    get_pc,
    hook,
    keybind,
)
from ui_utils import show_hud_message
from unrealsdk import find_all, find_object, logging, make_struct
from unrealsdk.hooks import Block
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

# the restock and the ammo choice used to be options too, but theres no good reason to turn
# either of them off, so theyre just part of the reload now
collect_on_clients = BoolOption(
    "Collect On Clients",
    False,
    description=(
        "Ask your partners machine to garbage collect before travelling, not just yours. Off"
        " because its the prime suspect for the crash on their end. Turn it on only if a partner"
        " freezes on the reload instead of crashing."
    ),
)

save_first = BoolOption(
    "Save Before Reloading",
    True,
    description=(
        "Commit your game before reloading, the way a real save quit does. Turning this off IS"
        " read only mode, the game stops saving entirely and every reload rolls missions back to"
        " your last save, so quest rewards stay farmable."
    ),
)

unstick_reloads = BoolOption(
    "Unstick Frozen Reloads",
    True,
    description=(
        "Stop map loads wedging the game at one frame a second. Shortly after each load, once"
        " the game is calm, it asks for one ordinary cleanup collect so the next load has"
        " nothing to trip on. If a load still freezes for ten seconds the mod cuts the leak that"
        " caused it, makes it collectable, and asks the engine to finish its cleanup right away."
        " Only a provably stuck load ever gets touched."
    ),
)


@hook("WillowGame.WillowPlayerController:CanSaveGame")
def _block_saving(
    _obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> tuple[type[Block], bool] | None:
    # with the switch off this mod IS the read only mod, it has to block every save and not just
    # ours, a quest turn in autosaves on the spot and that poisons the save the rollback reads
    if not save_first.value:
        return Block, False
    return None

# measure BEFORE writing, not after. reading the location back in the same call as the write just
# reads our own write and passes every time, so the hold was over in 8ms and the engine dragged
# you to the station anyway. settling is real time on target now, not a frame count.
_HOLD_SETTLED_SECONDS = 0.75  # continuously on target this long means it actually stuck
_HOLD_TOLERANCE = 50.0  # unreal units, close enough
_HOLD_MAX_SECONDS = 10.0  # give up rather than fight forever
_HOLD_APPLY_INTERVAL = 0.05  # 20 corrections a second still beats anything the game does
_HOLD_JUMP = 250.0  # a frame to frame move bigger than this is a teleport, not walking

# where WE were stood. kept on its own because the hold below needs a single target to measure
# against every frame.
_saved_location: Any = None

# where EVERYONE was stood, keyed by player name, so your partner gets put back at her own
# spot and not yours. player names survive the travel, pawns do not.
_saved_positions: dict = {}

# a partner needs their own watcher, everything on our side keys off OUR loading screen ending
# and theirs is still up then, so the correction landed on a pawn about to be replaced. one entry
# per player, waits for them to actually turn up.
_REMOTE_WINDOW_SECONDS = 45.0  # long enough for a slow machine to finish loading
_REMOTE_SETTLED_SECONDS = 1.0  # on target this long means it stuck
_REMOTE_APPLY_INTERVAL = 0.5  # a barrage of server corrections is what makes a client rubber band
_REMOTE_APPLIES_PER_PLACEMENT = 4  # every fresh placement earns a fresh go at putting them back
_REMOTE_APPLIES_TOTAL = 20  # hard stop, never fight someone forever
# keep watching for the late placement after theyre back, but not forever, dying at a new-u half
# a minute later is a real teleport and yanking them out of it would be us being the bug
_REMOTE_WATCH_AFTER_SETTLED = 10.0

_remote_watch: dict = {}
_remote_until = 0.0
# the player list failing is worth saying once and never again, see _players
_players_warned = False

# client side state. the reload is host only but read only isnt something one machine can do to
# another, so a client copy rolls its own missions back, see _loading_finished
_client_last_map = "?"
_client_rollback_at = 0.0
# how many times weve come back to look for a tracker that wasnt there yet. one second of waiting
# was a guess and on her machine it was the wrong guess, so ask again a few times before giving up
_client_rollback_tries = 0

# maps that arent a place. the loader is the little transition map the game sits on while it works
# out what to show you, and the menu is the menu. neither has a mission tracker, or missions, or a
# character stood in it, so a reload of one is not a reload of anything we care about
_NOT_A_PLACE = frozenset(("loader", "menumap"))

# waiting for the new map to come up after a travel
_pending_reload = False
# the map we left. if we come up somewhere else, the saved spots mean nothing there.
_travel_from_map = "?"
_map_is_up = False
_map_stable_since = 0.0
_travel_started_at = 0.0
_players_before = [0, []]
# true when this reload skipped saving and rolled missions back. rolling before the travel isnt
# enough on its own, the new map rebuilds the old status over it, so we roll again once its up
_rolled_back = False

# a travel we still owe the engine. the collect is only scheduled by the gc call, it actually
# runs on a later world tick, so travelling in the same frame would outrun it. this holds the
# station for a couple of ticks first.
_travel_station: Any = None
_gc_grace_ticks = 0

# reloading without a save rolls missions back, and thats not something to do to someone off a
# single stray keypress, so the first press just asks and the second one within the window goes
_readonly_confirm_at = 0.0
_READONLY_CONFIRM_SECONDS = 10.0

# hold state
_hold_until = 0.0
_hold_logged = False
_hold_on_target_since = 0.0
_hold_last_apply = 0.0
_hold_prev: Any = None
_hold_reached = False
# the hold can pass dead on target and the engine still moves you afterwards, only on the no save
# path. watching means were done holding but still looking for that one late teleport
_hold_watching = False

_tick_alive_logged = False
_tick_errors = 0  # consecutive, a clean frame resets it
_tick_errors_total = 0  # lifetime, only drives the log backoff


def _arm_population_reset() -> tuple[int, int]:
    # definition assets, not things in the world, and a plain bool write with no unrealscript call
    # in it, so none of the crash rules apply. exact=True keeps the archetype junk out anyway
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

        # enemies get their own class default too, for the same late streaming reason
        try:
            find_object(
                "WillowPopulationDefinition", "WillowGame.Default__WillowPopulationDefinition"
            ).bTotalResetOnLevelLoad = True
        except Exception:
            logging.warning(f"[{TITLE}] could not arm the enemy population default")

    # carve the dice chests back out, after the blanket pass so it actually sticks
    try:
        find_object("PopulationDefinition", _DICE_CHEST).bTotalResetOnLevelLoad = False
    except Exception:
        pass  # not loaded in this map, nothing to carve

    return lootables, enemies


def _unwrap(value):
    """out params come back alongside the return value, so sometimes its a tuple."""
    if isinstance(value, tuple):
        return value[0] if value else None
    return value


def _say(msg: str, duration: float = 2.5) -> None:
    # show_hud_message walks get_pc and the hud movie itself, so at the exact moments we most
    # want to talk, mid load, it can throw instead. the log is the fallback
    try:
        show_hud_message(TITLE, msg, duration)
    except Exception:  # noqa: BLE001
        logging.info(f"[{TITLE}] {msg}")


def _player_pools(owner: UObject, pawn: Any) -> list:
    """health and shield pools. ammo is left alone, a save quit gives you back what was in the save.

    health hangs off the pawn, shields dont hang off anything, the only road in is sweeping the
    pool class and matching each provider back to the player. ClientExpectedResourcePools looks
    like the right list but its empty on the host.
    """
    pools = []

    try:
        if pawn is not None and pawn.HealthPool.Data is not None:
            pools.append(pawn.HealthPool.Data)
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] health pool lookup failed: {e!r}")

    try:
        for pool in find_all("ShieldResourcePool", exact=True):
            try:
                prov = pool.AssociatedProvider
                if prov is None:
                    continue
                if prov == owner or (pawn is not None and prov == pawn):
                    pools.append(pool)
                    continue
                # providers are usually controllers, so walk one step to their pawn
                try:
                    if pawn is not None and prov.Pawn == pawn:
                        pools.append(pool)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] shield pool sweep failed: {e!r}")

    return pools


def _restock_player(pc_or_owner: UObject) -> str:
    """health, action skill and shield back. a save quit gets these for free by re-reading your
    character off disk, we keep it live in memory so nothing resets on its own.
    """
    done = []

    # up first. if you were crawling around in fight for your life when the map went, you come
    # back on your feet, because thats what a save quit does to you.
    try:
        downed = pc_or_owner.Pawn
        if downed is not None and _unwrap(downed.IsInjured(False)):
            pc_or_owner.Resurrect()
            done.append("revived")
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] revive failed: {e!r}")

    try:
        pawn = pc_or_owner.Pawn
    except Exception:  # noqa: BLE001
        pawn = None

    # the pools are the real mechanism, health and shields are pool driven and writing anything
    # else gets overwritten by them a tick later
    filled = 0
    for pool in _player_pools(pc_or_owner, pawn):
        try:
            # one bool, False means the live max with modifiers. no out param on this one, its
            # checked against the bytecode, passing a fake one throws on every pool
            top = _unwrap(pool.GetMaxValue(False))
            if top:
                pool.SetCurrentValue(float(top))
                filled += 1
        except Exception:  # noqa: BLE001
            continue
    if filled:
        done.append(f"{filled} pool(s)")

    # and the engine side health var too, belt and braces, the pool syncs it anyway
    try:
        if pawn is not None:
            max_hp = _unwrap(pawn.GetMaxHealth(0.0))
            if max_hp:
                pawn.SetHealth(float(max_hp))
                done.append(f"health->{float(max_hp):.0f}")
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] health restore failed: {e!r}")

    # action skill. ask first, the game knows when it is not allowed.
    try:
        if _unwrap(pc_or_owner.CanResetActionSkill(False)):
            pc_or_owner.ResetActionSkill()
            done.append("action skill")
        else:
            pc_or_owner.ResetSkillCooldown()
            done.append("skill cooldown")
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] action skill reset failed: {e!r}")

    return ", ".join(done) if done else "nothing"


def _restock_everyone(pc: UObject) -> None:
    """everyone gets topped up, not just the host. pools are replicated so this reaches them."""
    for pri in _players(pc):
        try:
            owner = pri.Owner
            if owner is None:
                continue
            logging.info(f"[{TITLE}] restocked {pri.PlayerName}: {_restock_player(owner)}")
        except Exception as e:  # noqa: BLE001
            # somebody quietly getting skipped here reads exactly like the restock not working,
            # and thats a whole evening of looking in the wrong place
            logging.warning(f"[{TITLE}] could not restock a player: {e!r}")
            continue


def _mission_path(mission: Any) -> str:
    try:
        return str(mission._path_name())
    except Exception:  # noqa: BLE001
        return str(mission)


def _rollback_missions(pc: UObject) -> bool:
    """walk every mission back to what the save file says, this is the read only farming piece.

    nothing gets re-read from disk on our reload so turn ins stuck even with read only on. the
    game keeps a parsed copy of the save around, so anything thats drifted from it goes back
    through the tracker, which replicates so your partner sees it too. runs before the travel so
    the new map spawns npcs already reading the rolled back state.

    returns false only when the world wasnt ready, thats worth coming back for.
    """
    try:
        # the tracker hangs off the replication info, not the game info. and the replication info
        # itself can be missing early in a map on a client, thats not a failure, its early, so say
        # it quietly and let the caller come back
        world = pc.WorldInfo
        gri = world.GRI if world is not None else None
        tracker = gri.MissionTracker if gri is not None else None
        if tracker is None:
            logging.info(f"[{TITLE}] no mission tracker yet, the map isnt up on this side")
            return False

        save = pc.GetCachedSaveGame()
        if save is None:
            logging.warning(f"[{TITLE}] no cached save, missions stay as they are")
            return False

        playthrough = int(_unwrap(pc.GetCurrentPlaythrough()))

        # the PlayThroughNumber field inside these entries is junk, a three playthrough save
        # logged [0, 0, 0]. the array POSITION is the playthrough, so index straight in,
        # clamped in case an old save has fewer entries than the playthrough youre on
        saved_pts = list(save.MissionPlaythroughs)
        saved_status: dict = {}
        saved_idx = -1
        if saved_pts:
            saved_idx = min(playthrough, len(saved_pts) - 1)
            for entry in saved_pts[saved_idx].MissionData:
                if entry.MissionDef is not None:
                    saved_status[_mission_path(entry.MissionDef)] = int(entry.Status)
        logging.info(
            f"[{TITLE}] save has {len(saved_pts)} playthrough slot(s), using index"
            f" {saved_idx} for playthrough {playthrough}, {len(saved_status)} mission(s) in it"
        )

        # the tracker only ever holds the playthrough youre on so theres nothing to filter here.
        # the controllers own array does need filtering and its entries all claim playthrough
        # zero, an exact match there skips every one of them, thats the old rolled 0 of 0
        live = list(tracker.MissionList)

        if not saved_status and live:
            # an empty saved map with a full live list would roll EVERYTHING to not started and
            # wipe the whole playthrough. that smells like the playthrough filter mismatching,
            # not a genuinely fresh character, so refuse rather than nuke
            logging.warning(
                f"[{TITLE}] save file has no missions for playthrough {playthrough} but"
                f" {len(live)} are live, not touching anything"
            )
            # deliberate, not early. coming back in a second would only print this again
            return True

        rolled = 0
        compared = 0
        for i, entry in enumerate(live):
            mission = entry.MissionDef
            if mission is None:
                continue
            compared += 1
            # a mission the save has never heard of was picked up after that save, and a
            # real save quit would forget it completely, so not started is the honest value
            want = saved_status.get(_mission_path(mission), 0)
            have = int(entry.Status)
            if have == want:
                continue
            try:
                # SetMissionStatus replicates but it quietly refuses to move a completed mission
                # backwards, so write the struct directly after it, ours last so it wins. read it
                # back off the tracker so the log says what stuck, not what we asked for
                tracker.SetMissionStatus(mission, want, pc)
                entry.Status = want
                now_is = int(tracker.MissionList[i].Status)
                rolled += 1
                logging.info(
                    f"[{TITLE}] rolling {_mission_path(mission)} from {have} to {want},"
                    f" tracker now says {now_is}"
                )
            except Exception as e:  # noqa: BLE001
                logging.warning(
                    f"[{TITLE}] could not roll back {_mission_path(mission)}: {e!r}"
                )

        # the tracker dies with the level and the next one rebuilds from the controllers list, so
        # rolling only the tracker gets quietly overwritten by the travel. write both
        mirrored = 0
        live_pts = list(pc.MissionPlaythroughs)
        if live_pts:
            for entry in live_pts[min(playthrough, len(live_pts) - 1)].MissionList:
                mission = entry.MissionDef
                if mission is None:
                    continue
                want = saved_status.get(_mission_path(mission), 0)
                if int(entry.Status) == want:
                    continue
                try:
                    entry.Status = want
                    mirrored += 1
                except Exception as e:  # noqa: BLE001
                    logging.warning(
                        f"[{TITLE}] could not mirror {_mission_path(mission)}: {e!r}"
                    )

        # the "of" number matters, rolled 0 of 50 right after a turn in means the games cached
        # save got refreshed before the write was blocked, and id need to snapshot it myself
        logging.info(
            f"[{TITLE}] rolled {rolled} of {compared} live mission(s) back to the save file"
            f" ({len(saved_status)} in the save), mirrored {mirrored} into the persistent list"
        )
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] mission rollback failed: {e!r}")
        # something actually went wrong, which is different from being early, so dont come back
        # and print it another four times
        return True
    return True


def _force_gc(pc: UObject) -> str:
    """ask for a collect before travelling, here and optionally on everyone elses machine.

    the client half is off by default, a purge on a leaked world just buys another walk of the
    leaked graph and thats where the partner side crash landed. its an rpc so it doesnt need the
    mod installed on their end either way.

    this only SCHEDULES the purge, it returns in under a millisecond and the engine runs it on a
    later tick, so the keybind parks the station for two ticks instead of travelling right away.
    """
    done = []

    try:
        # True means a full purge, not just a mark
        pc.WorldInfo.ForceGarbageCollection(True)
        done.append("host")
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] host gc failed: {e!r}")

    if not collect_on_clients.value:
        # said out loud rather than just left out, so the log tells us which way the switch was
        # set on the run that crashed
        done.append("clients skipped")
        return ", ".join(done) if done else "nothing"

    others = 0
    for pri in _players(pc):
        try:
            owner = pri.Owner
            if owner is None or owner is pc:
                continue
            owner.ClientForceGarbageCollection()
            others += 1
        except Exception as e:  # noqa: BLE001
            # this one reaches into someone elses game, it does not get to fail quietly
            logging.warning(f"[{TITLE}] could not ask a client to collect: {e!r}")
            continue
    if others:
        done.append(f"{others} client(s)")

    return ", ".join(done) if done else "nothing"


def _everyone_can_travel(pc: UObject, station: UObject) -> tuple[bool, str]:
    """can every player in the session reach this station. advisory only, the theory that dlc
    stations were booting people turned out to be wrong, it just rules it out in the log.
    """
    try:
        name = station.Name
    except Exception:  # noqa: BLE001
        return True, "could not read the station name, carrying on"

    blocked = []
    for pri in _players(pc):
        try:
            owner = pri.Owner
            if owner is None:
                continue
            if _unwrap(owner.IsStationToUninstalledDlc(False, name)):
                blocked.append(str(pri.PlayerName))
        except Exception:  # noqa: BLE001
            continue

    if blocked:
        return False, f"{', '.join(blocked)} cannot reach {name}, it is dlc they do not have"
    return True, f"everyone can reach {name}"


def _map_now(pc: UObject) -> str:
    try:
        return str(pc.WorldInfo.GetMapName(False))
    except Exception:  # noqa: BLE001
        return "?"


def _players(pc: UObject) -> list:
    """everyone in the session, host and clients. GRI.PRIArray is plain engine, credit to
    commander for showing its the route that works, but nothing here imports it.
    """
    global _players_warned
    try:
        gri = pc.WorldInfo.GRI
        if gri is None:
            return []
        return list(gri.PRIArray)
    except Exception as e:  # noqa: BLE001
        # an empty list here turns the positions, the restock and the collect all into no ops at
        # once, so it has to say something. once only though, this gets called every frame while
        # the watcher is up and carpeting the log is how the tick jammed the mod once already
        if not _players_warned:
            _players_warned = True
            logging.error(f"[{TITLE}] cannot read the player list, this reload will do less: {e!r}")
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
            name = str(pri.PlayerName)
            # names are the key because pawns dont survive a travel and names do. two people
            # sharing one would quietly share a spot, so say it rather than let it look like a bug
            if name in out:
                logging.warning(
                    f"[{TITLE}] two players are both called {name}, only one of them gets put back"
                )
            out[name] = (loc.X, loc.Y, loc.Z, rot.Pitch, rot.Roll, rot.Yaw)
        except Exception as e:  # noqa: BLE001
            # missing off this list is the difference between someone being put back and someone
            # waking up at the station wondering why it only works for everyone else
            logging.warning(f"[{TITLE}] could not note down where someone was stood: {e!r}")
            continue
    return out


def _my_name(pc: UObject) -> str | None:
    try:
        return str(pc.PlayerReplicationInfo.PlayerName)
    except Exception:  # noqa: BLE001
        return None


def _to_structs(saved: tuple) -> tuple:
    # own structs, not views. Location off a pawn points at the pawns own memory, so writing its
    # fields moves the pawn behind the engines back before the real move even runs
    return (
        make_struct("Vector", X=saved[0], Y=saved[1], Z=saved[2]),
        make_struct("Rotator", Pitch=saved[3], Roll=saved[4], Yaw=saved[5]),
    )


def _apply_my_position(pc: UObject) -> None:
    """put US back. free to do as often as we like, no network in the way."""
    if _saved_location is None or pc.Pawn is None:
        return
    name = _my_name(pc)
    saved = _saved_positions.get(name) if name else None
    if saved is None:
        # the per player note is missing but the hold target is not, so use that and keep
        # whatever way were facing
        saved = (_saved_location[0], _saved_location[1], _saved_location[2], 0, 0, 0)
        loc, _rot = _to_structs(saved)
        try:
            pc.NoFailSetPawnLocation(pc.Pawn, loc)
        except Exception as e:  # noqa: BLE001
            logging.warning(f"[{TITLE}] could not move myself: {e!r}")
        return

    # the hold owns where we are going, the note only supplies the facing
    loc, rot = _to_structs(
        (_saved_location[0], _saved_location[1], _saved_location[2], saved[3], saved[4], saved[5])
    )
    try:
        pc.NoFailSetPawnLocation(pc.Pawn, loc)
        pc.ClientSetRotation(rot)
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] could not move myself: {e!r}")


def _place_remote(owner: UObject, name: str, saved: tuple, loud: bool) -> None:
    """put someone else back, and say in the log what actually happened.

    two calls on purpose. the server move is the one that counts but the client is predicting its
    own walk and would undo it on its next move, so ClientSetLocation goes out too, thats the
    engines own stop predicting rpc. both get logged, one failing quietly looks identical to the
    whole thing not working.
    """
    loc, rot = _to_structs(saved)

    server_ok = True
    try:
        owner.NoFailSetPawnLocation(owner.Pawn, loc)
    except Exception as e:  # noqa: BLE001
        server_ok = False
        logging.warning(f"[{TITLE}] server side move failed for {name}: {e!r}")

    client_ok = True
    try:
        owner.ClientSetLocation(loc, rot)
    except Exception as e:  # noqa: BLE001
        client_ok = False
        try:
            owner.ClientSetRotation(rot)
        except Exception:  # noqa: BLE001
            pass
        if loud:
            logging.warning(f"[{TITLE}] no ClientSetLocation for {name}: {e!r}")

    if loud:
        logging.info(
            f"[{TITLE}] correcting {name} to ({saved[0]:.0f}, {saved[1]:.0f}, {saved[2]:.0f})"
            f" (server {'ok' if server_ok else 'failed'}, client {'ok' if client_ok else 'failed'})"
        )


def _arm_remote_watch(pc: UObject, now: float) -> None:
    """start watching everyone else home. their clock, not ours."""
    global _remote_until

    _remote_watch.clear()
    _remote_until = 0.0
    if not _saved_positions:
        return

    my_name = _my_name(pc)
    for pri in _players(pc):
        try:
            name = str(pri.PlayerName)
            # match on the controller, not the name, two people can share a name and our own name
            # lookup can fail. either one puts US in this list and then the watcher and the hold
            # fight over the same pawn
            if pri.Owner == pc:
                continue
        except Exception:  # noqa: BLE001
            continue
        if my_name is not None and name == my_name:
            continue
        saved = _saved_positions.get(name)
        if saved is None:
            continue
        _remote_watch[name] = {
            "target": saved,
            "prev": None,
            "applies": 0,  # since the last placement
            "total": 0,  # this reload
            "last_apply": 0.0,
            "on_target_since": 0.0,
            "settled": False,
            "settled_at": 0.0,
            "seen": False,
            "ready": False,
            "restocked": False,
            "done": False,
        }

    if _remote_watch:
        _remote_until = now + _REMOTE_WINDOW_SECONDS
        logging.info(
            f"[{TITLE}] watching {', '.join(_remote_watch)} home,"
            f" up to {_REMOTE_WINDOW_SECONDS:.0f}s"
        )


def _note_remote_ready(obj: UObject) -> None:
    """their loading screen just went away, so theyre about to get dropped at the station."""
    try:
        name = str(obj.PlayerReplicationInfo.PlayerName)
    except Exception:  # noqa: BLE001
        return
    state = _remote_watch.get(name)
    if state is None or state["ready"]:
        return
    state["ready"] = True
    # whatever we spent correcting a pawn that was still loading was spent on nothing, so hand
    # back a clean budget for the placement thats coming
    state["applies"] = 0
    logging.info(f"[{TITLE}] {name} finished loading")


def _remote_stand_down(why: str) -> None:
    global _remote_until
    if _remote_watch:
        # someone we already gave up on got their own warning, dont say it twice
        unfinished = [
            n for n, s in _remote_watch.items() if not s["settled"] and not s["done"]
        ]
        if unfinished:
            logging.warning(f"[{TITLE}] {', '.join(unfinished)} never made it back ({why})")
        else:
            logging.info(f"[{TITLE}] done watching the others ({why})")
    _remote_watch.clear()
    _remote_until = 0.0


def _remote_tick(pc: UObject, now: float) -> None:
    """one frame of putting everyone else back."""
    global _remote_until

    if now >= _remote_until:
        _remote_stand_down("ran out of time")
        return

    my_name = _my_name(pc)
    working = 0

    for pri in _players(pc):
        try:
            name = str(pri.PlayerName)
        except Exception:  # noqa: BLE001
            continue
        if my_name is not None and name == my_name:
            continue
        state = _remote_watch.get(name)
        if state is None or state["done"]:
            continue
        if state["settled"] and now - state["settled_at"] >= _REMOTE_WATCH_AFTER_SETTLED:
            # back, stayed back, lookout done. nothing to warn about so nothing gets said
            state["done"] = True
            continue
        working += 1

        try:
            owner = pri.Owner
            if owner == pc:
                continue
            pawn = None if owner is None else owner.Pawn
            if pawn is None:
                # still on their loading screen. their pawn turns up when they get here, and
                # this is exactly the wait the old code never did
                continue
            here = pawn.Location
            pos = (here.X, here.Y, here.Z)
        except Exception:  # noqa: BLE001
            continue

        prev = state["prev"]
        state["prev"] = pos
        jumped = prev is not None and any(
            abs(pos[i] - prev[i]) > _HOLD_JUMP for i in range(3)
        )

        if not state["seen"]:
            state["seen"] = True
            target = state["target"]
            logging.info(
                f"[{TITLE}] {name} is in the map at ({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}),"
                f" want ({target[0]:.0f}, {target[1]:.0f}, {target[2]:.0f})"
            )

        if jumped:
            # nobody covers that much ground in one frame, so the engine moved them, which on this
            # side of a reload means they just landed and got dropped at the station
            if state["settled"]:
                # theyd already made it back once, so this is the late placement, the same one
                # that yanks us to the station after our own hold lets go. take the spot from
                # just before the jump, that way someone who wandered off keeps their wander
                state["target"] = (prev[0], prev[1], prev[2], *state["target"][3:])
                state["settled"] = False
            state["applies"] = 0
            state["on_target_since"] = 0.0
            logging.info(
                f"[{TITLE}] {name} got moved to ({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}),"
                " grabbing them back"
            )

        if (state["ready"] or jumped) and not state["restocked"]:
            # the restock at map up hit whatever pawn they had while still loading, and travel
            # hands them a new one, so do it again now theyre really here. twice costs nothing
            state["restocked"] = True
            try:
                logging.info(f"[{TITLE}] restocked {name} on arrival: {_restock_player(owner)}")
            except Exception as e:  # noqa: BLE001
                logging.warning(f"[{TITLE}] arrival restock failed for {name}: {e!r}")

        target = state["target"]
        on_target = all(abs(pos[i] - target[i]) < _HOLD_TOLERANCE for i in range(3))

        if on_target:
            if not state["on_target_since"]:
                state["on_target_since"] = now
            elif (
                not state["settled"]
                and now - state["on_target_since"] >= _REMOTE_SETTLED_SECONDS
            ):
                state["settled"] = True
                state["settled_at"] = now
                logging.info(
                    f"[{TITLE}] {name} is back where they were"
                    f" after {state['total']} correction(s)"
                )
            continue

        state["on_target_since"] = 0.0

        if state["settled"]:
            # theyve been home and now theyre off under their own steam, thats theirs to do. only
            # a teleport sized jump above gets us involved again
            continue

        if now - state["last_apply"] < _REMOTE_APPLY_INTERVAL:
            continue

        if (
            state["applies"] >= _REMOTE_APPLIES_PER_PLACEMENT
            or state["total"] >= _REMOTE_APPLIES_TOTAL
        ):
            state["done"] = True
            logging.warning(
                f"[{TITLE}] stopped trying to put {name} back after"
                f" {state['total']} correction(s), leaving them where they are"
            )
            continue

        state["last_apply"] = now
        state["applies"] += 1
        state["total"] += 1
        _place_remote(owner, name, target, state["total"] == 1)

    if working == 0:
        # nobody left to watch. that is either everyone home or everyone given up on, and the
        # lines above already said which, so dont claim anything here
        _remote_watch.clear()
        _remote_until = 0.0


@keybind(
    "Co-op Save Quit",
    key="F6",
    description="Reload the map, fresh loot, nobody gets dropped from your game.",
)
def lobby_safe_reload() -> None:
    # anything that escapes one of our entry points goes back into the engine, and a throw partway
    # through would leave the reload half armed with nothing on screen to say why. body in its own
    # function, this end just catches
    global _pending_reload, _travel_station, _hold_until, _hold_watching
    try:
        _reload_body()
    except Exception as e:  # noqa: BLE001
        logging.error(f"[{TITLE}] reload failed: {e!r}")
        _say("Something went wrong, nothing reloaded.")
        _pending_reload = False
        _travel_station = None
        _hold_until = 0.0
        _hold_watching = False
        _remote_stand_down("the reload threw")


def _reload_body() -> None:
    global _saved_location, _saved_positions
    global _pending_reload, _travel_from_map, _travel_started_at
    global _map_stable_since, _hold_until, _hold_watching, _tick_errors
    global _map_is_up, _travel_station, _gc_grace_ticks
    global _readonly_confirm_at, _remote_until

    # the watch after a finished hold does not count as busy, the reload is done by then and
    # blocking the key for ten extra seconds would just slow the farm down
    if _pending_reload or (_hold_until and not _hold_watching):
        _say("Already reloading, hang on.")
        logging.info(f"[{TITLE}] ignored, a reload is already in progress")
        return

    pc = get_pc(possibly_loading=True)
    if pc is None:
        _say("Not in game.")
        return

    world_info = pc.WorldInfo

    if world_info.NetMode == NM_CLIENT:
        _say("Host only, someone else is hosting this game.")
        return

    game = world_info.Game
    if game is None:
        _say("No game info, cannot travel.")
        return

    # is a save actually going to happen. our own hook blocks CanSaveGame while the switch is
    # off, and a standalone read only mod can block it from the outside too. our SaveGame call
    # would sail straight past those hooks so we have to ask first rather than find out after
    can_save = True
    try:
        can_save = bool(_unwrap(pc.CanSaveGame()))
    except Exception:  # noqa: BLE001
        pass
    will_save = save_first.value and can_save

    if not will_save:
        # no save means the reload rolls missions back, and losing progress off one stray press
        # would be nasty, so the first press only asks
        now = time.monotonic()
        if now - _readonly_confirm_at > _READONLY_CONFIRM_SECONDS:
            _readonly_confirm_at = now
            why = "Read only is on" if save_first.value else "Saving is turned off"
            # saving happens on whoever owns the character, we cant reach into that from here, so
            # say it out loud instead of letting someone assume their partner is covered
            others = len(_players(pc)) > 1
            extra = " It only covers you, theyd need this mod too." if others else ""
            # keep the box up exactly as long as the second press still counts, when it fades
            # the window is shut too
            _say(
                f"{why}, so this rolls missions back to your last save.{extra}"
                " Press again if youre sure.",
                _READONLY_CONFIRM_SECONDS,
            )
            return
        _readonly_confirm_at = 0.0

    # the station we last saved at. usually the one in the map youre stood in, because BL2 saves
    # at new-u points and map entrances as well as fast travel ones.
    try:
        station = pc.GetSavedTravelStation(pc.GetCachedSaveGame())
    except Exception as e:
        logging.error(f"[{TITLE}] could not find a station: {e!r}")
        station = None

    if station is None:
        _say("No travel station for this map, cannot reload here.")
        return

    ok, why = _everyone_can_travel(pc, station)
    if not ok:
        logging.warning(f"[{TITLE}] dlc check: {why}")
    else:
        logging.info(f"[{TITLE}] dlc check: {why}")

    logging.info(f"[{TITLE}] forced a collect on {_force_gc(pc)}")

    # arm it BEFORE we travel, the flag is read on the way in
    lootables, enemies = _arm_population_reset()
    logging.info(f"[{TITLE}] armed {lootables} lootable + {enemies} enemy population defs")

    _saved_location = None
    _saved_positions = {}
    _remote_watch.clear()
    _remote_until = 0.0
    if restore_position.value:
        # everyone gets noted down including us, and outside the pawn check below, our own pawn
        # going missing for a moment shouldnt take everyone elses spot with it
        _saved_positions = _capture_positions(pc)
        if pc.Pawn is None:
            logging.warning(f"[{TITLE}] no pawn, cannot remember where you were")
        else:
            # copy the NUMBERS out, not the structs. these point at the pawns own memory and the
            # pawn does not survive a travel.
            loc = pc.Pawn.Location
            _saved_location = (loc.X, loc.Y, loc.Z)
        mine = (
            "no pawn"
            if _saved_location is None
            else f"({_saved_location[0]:.0f}, {_saved_location[1]:.0f}, {_saved_location[2]:.0f})"
        )
        logging.info(f"[{TITLE}] remembered {len(_saved_positions)} player(s), mine {mine}")

    # a save quit commits before it reloads so we do too, unless nothings going to save, then the
    # whole point is not committing and the missions get walked back to the save file instead
    global _rolled_back
    if will_save:
        try:
            pc.SaveGame()
        except Exception as e:
            logging.error(f"[{TITLE}] SaveGame() failed: {e!r}")
            _say("Couldnt save, so im not travelling.")
            return
    else:
        logging.info(
            f"[{TITLE}] not saving ({'read only' if save_first.value else 'option off'}),"
            " rolling missions back"
        )
        _rollback_missions(pc)
        _rolled_back = True

    try:
        names_now = [str(pri.PlayerName) for pri in _players(pc)]
        _players_before[0] = len(names_now)
        _players_before[1] = names_now
    except Exception:  # noqa: BLE001
        pass

    _map_is_up = False
    _pending_reload = True
    _travel_from_map = _map_now(pc)
    _travel_started_at = time.monotonic()
    _map_stable_since = _travel_started_at
    _hold_until = 0.0
    _hold_watching = False
    _tick_errors = 0

    # the collect above only got scheduled, the engine runs it on a later world tick. fire the
    # travel in this same frame and the transition outruns the purge it was meant to shrink, so
    # the station waits a couple of ticks instead
    _travel_station = station
    _gc_grace_ticks = 2
    logging.info(f"[{TITLE}] travelling to {station.Name} after the collect gets a tick")


# sdk hooks want a COLON before the function name, the legacy mods all use a dot and copying that
# meant none of these ever fired. the loading movie going away IS the map being up, and it works
# travelling to the same map or a different one, which the map name check couldnt.
@hook("WillowGame.WillowPlayerController:WillowClientDisableLoadingMovie")
def _loading_finished(
    _obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    # same rule as the key and the tick, nothing gets out of here into the engine
    try:
        _loading_body(_obj)
    except Exception as e:  # noqa: BLE001
        logging.error(f"[{TITLE}] loading hook failed: {e!r}")


def _loading_body(_obj: UObject) -> None:
    global _map_is_up, _map_stable_since, _client_last_map, _client_rollback_at
    global _client_rollback_tries, _settle_collect_at

    # this is a client rpc so the host sees one per remote player too. only ours counts for the
    # settle wait, but theirs is the one honest signal that their machine finished loading
    try:
        mine = _obj == get_pc(possibly_loading=True)
    except Exception:  # noqa: BLE001
        return

    if not mine:
        if _remote_until:
            _note_remote_ready(_obj)
        return

    # every local arrival arms the calm play collect, boot loads and plain travels included. the
    # leftovers that wedge a reload were left behind by whatever load came before it, so this has
    # to arm here and not just on our own key
    if unstick_reloads.value:
        _settle_collect_at = time.monotonic() + _SETTLE_COLLECT_DELAY

    if _pending_reload:
        _map_is_up = True
        # start the settle timer HERE, not at travel start. comparing against travel start made
        # the half second wait meaningless, every load takes longer than that anyway.
        _map_stable_since = time.monotonic()
        logging.info(f"[{TITLE}] loading movie ended")
        return

    # a map came up we didnt ask for. on a client thats the only sign the host reloaded, and the
    # rollback has to happen over here. same map name means a reload and not someone walking off
    try:
        was = _client_last_map
        _client_last_map = _map_now(_obj)
        if (
            not save_first.value
            and _obj.WorldInfo.NetMode == NM_CLIENT
            and was == _client_last_map
            and was != "?"
            # the loading screen ends twice on the loader map on the way in, which reads as the
            # same map coming up again and armed this on every launch, on a map with no tracker
            and _client_last_map.lower() not in _NOT_A_PLACE
        ):
            # give the replicated state a moment to arrive before reading it, same reason the
            # host waits half a second after its own loading screen goes
            _client_rollback_at = time.monotonic() + 1.0
            _client_rollback_tries = 0
            logging.info(
                f"[{TITLE}] {_client_last_map} reloaded under us and saving is off,"
                " rolling my own missions back in a moment"
            )
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] client side map check failed: {e!r}")


# the wedge breaker. on a map load the engine has to gc the map its leaving, and sometimes it
# cant, a rooted ObjectSerializer is still holding the old maps sub levels through cross level
# references. rooted means never collect, so it retries the full sweep every second forever, and
# thats the one frame a second freeze. the way out, proven on a real wedge: cut the refs, unroot
# the leftover worlds and the serializers, then ask for the collect. the cut alone never rescues
# anything, the serializer holds levels through native arrays the property system cant see.
#
# 2.10.0 stripped the marks on every load so a wedge could never form, and it crashed the very
# first load. the engine holds some healthy objects through plain c++ pointers, so for those the
# mark is the only thing keeping them alive. mid wedge the holder really is dead so unrooting is
# safe, on a healthy game nothing gets touched, the census only looks and logs.
#
# every field wedge opened with a frozen stretch of about twenty five seconds, one giant engine
# op with no tick or hook inside it, so nothing can act sooner than the first frame after it.
# wedges fire on plain map loads too so this watches every frame from launch.

_WEDGE_LOW_FPS = 5.0  # under this the game is not slow, its finished
_WEDGE_LOW_STREAK_NEEDED = 4  # seconds in a row it has to stay down there
_WEDGE_BIG_STALL = 10.0  # a gap with no frames at all this long means a load wedged
_WEDGE_ESCALATE_AFTER = 10.0  # still on the floor this long after an attempt, try the next
_WEDGE_RECOVERED_FPS = 20.0  # back above this after an attempt means we won
_WEDGE_LAST_STAGE = 2
_WEDGE_SURE_STALL = 20.0  # a freeze this long has no innocent explanation
_WEDGE_SURE_STREAK = 2  # so the crawl after it only needs this many seconds to convict

# the calm play collect. every wedge was a reload tripping over leftovers the load before it never
# let go of, so once the game is calm again after an arrival we ask for one ordinary collect. its
# the same call the game makes at its own transitions, nothing gets unrooted or cut
_SETTLE_COLLECT_DELAY = 10.0  # let the arrival finish streaming and spawning first
_SETTLE_CALM_FPS = 30.0  # only a game thats visibly fine gets asked
_SETTLE_GIVE_UP = 90.0  # a slow load plus a rescued wedge can eat a minute before its calm

# unreals never collect mark. the diag cleared the wrong bit once and its log read exactly like a
# success, so every unroot below reads the flags back and says whether it actually took
_ROOT_SET = 0x4000

# the chain the engine log printed sixty times over on the wedge. a cross level reference
# container is how one streamed sub level points into another, and every hop of the chain holding
# the dead map down was this one field on one of these
_CROSS_LEVEL = "GBXCrossLevelReferenceContainer"
_CROSS_LEVEL_FIELD = "CrossLevelObjectRef"

# the anchor. rooted engine furniture thats alive from the menu screen on, and on a wedge its
# whats holding the dead maps levels, partly through refs the property system cant even see
_HOLDER = "ObjectSerializer"

_wedge_last_sample = 0.0
_wedge_ticks = 0
_wedge_fps = 0.0
_wedge_saw_stall = False
_wedge_stall_gap = 0.0
_wedge_low_streak = 0
_settle_collect_at = 0.0
_wedge_stage = 0
_wedge_next_attempt_at = 0.0
# the watch runs on every frame of the whole session, so it gets its own error handling and its
# own kill switch, it must never be able to abandon a reload it had nothing to do with
_wedge_errors = 0
_wedge_dead = False


def _path_of(obj: Any) -> str:
    # the full path the way the engine log writes one. a bare name is useless here, two levels
    # can both own a BlockingMeshActor_1
    try:
        return str(obj._path_name())
    except Exception:  # noqa: BLE001
        pass
    try:
        outer = obj.Outer
        return (f"{outer.Name}." if outer is not None else "") + str(obj.Name)
    except Exception:  # noqa: BLE001
        return "<unnamed>"


def _is_rooted(obj: Any) -> bool:
    try:
        return bool(int(obj.ObjectFlags) & _ROOT_SET)
    except Exception:  # noqa: BLE001
        return False


def _cut_cross_level_refs() -> int:
    """null the one property the engine log names, wherever it still points at something.

    exact off on purpose, thats how the diag ran it on the proven wedge, and its safe because we
    only read and write a property here, the crash rule is about CALLING things on archetypes.
    """
    try:
        found = list(find_all(_CROSS_LEVEL, exact=False))
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] could not look for {_CROSS_LEVEL}: {e!r}")
        return 0

    cut = 0
    for obj in found:
        try:
            value = getattr(obj, _CROSS_LEVEL_FIELD)
        except Exception:  # noqa: BLE001
            continue
        if value is None or value == []:
            continue
        # both ends, full paths, before the cut. if this ever wedges differently the log is the
        # only record of what was holding what
        here = _path_of(obj)
        there = _path_of(value) if hasattr(value, "Name") else repr(value)[:120]
        try:
            setattr(obj, _CROSS_LEVEL_FIELD, [] if isinstance(value, list) else None)
        except Exception as e:  # noqa: BLE001
            logging.warning(f"[{TITLE}] {here} would not let go: {e!r}")
            continue
        cut += 1
        logging.info(f"[{TITLE}] cut {here} -> {there}")
    return cut


def _unroot_stale_worlds(pc: Any) -> int:
    """take the never collect mark off every world except the one were loading into, that ones
    rooted for a good reason. its name reads fine all through a wedge, the world is half up the
    whole time, it just cant finish.
    """
    here = _map_now(pc).lower()
    try:
        worlds = list(find_all("World", exact=True))
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] could not list worlds: {e!r}")
        return 0

    freed = 0
    for world in worlds:
        try:
            name = str(world.Outer.Name) if world.Outer else str(world.Name)
        except Exception:  # noqa: BLE001
            continue
        if not _is_rooted(world):
            continue
        if name.lower() == here:
            continue
        try:
            before = int(world.ObjectFlags)
            world.ObjectFlags = before & ~_ROOT_SET
            after = int(world.ObjectFlags)
            worked = "cleared" if not after & _ROOT_SET else "DID NOT TAKE, still rooted"
            logging.info(f"[{TITLE}] {name} unroot {worked}, flags {before:#x} -> {after:#x}")
            if not after & _ROOT_SET:
                freed += 1
        except Exception as e:  # noqa: BLE001
            logging.warning(f"[{TITLE}] {name} could not be unrooted: {e!r}")
    return freed


def _unroot_serializers() -> int:
    """make the serializers collectable, this is the half that actually won. cutting what the
    property system can see isnt enough, the serializer holds the dead levels through native
    arrays as well, so the object itself has to be allowed to die.
    """
    try:
        found = list(find_all(_HOLDER, exact=False))
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] could not look for {_HOLDER}: {e!r}")
        return 0

    freed = 0
    for obj in found:
        if not _is_rooted(obj):
            continue
        try:
            name = str(obj.Name)
        except Exception:  # noqa: BLE001
            name = "<unnamed>"
        try:
            before = int(obj.ObjectFlags)
            obj.ObjectFlags = before & ~_ROOT_SET
            after = int(obj.ObjectFlags)
            worked = "cleared" if not after & _ROOT_SET else "DID NOT TAKE, still rooted"
            logging.info(f"[{TITLE}] {name} unroot {worked}, flags {before:#x} -> {after:#x}")
            if not after & _ROOT_SET:
                freed += 1
        except Exception as e:  # noqa: BLE001
            logging.warning(f"[{TITLE}] {name} could not be unrooted: {e!r}")
    return freed


def _ask_for_collect(pc: Any) -> bool:
    """ask the engine to collect at its next safe moment. without this the unroot still works but
    the engine takes its own sweet time noticing, about 170 seconds at one fps with the fix
    already done.
    """
    try:
        pc.WorldInfo.ForceGarbageCollection(True)
        logging.info(f"[{TITLE}] asked the engine for a full collect right now")
        return True
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] could not ask for a collect: {e!r}")
        return False


_census_last = 0.0


def _travel_census(pc: Any) -> None:
    """log whats rooted as a map load starts, and touch absolutely nothing.

    stripping marks here instead of looking is what crashed 2.10.0 on its first load, the mark is
    the only thing keeping some live objects alive. so this just leaves a line to read if a wedge
    ever forms again.
    """
    global _census_last
    if not unstick_reloads.value:
        return
    try:
        now = time.monotonic()
        if now - _census_last < 5.0:
            # one travel fires this from more than one place, once is plenty
            return
        _census_last = now
        rooted: list[str] = []
        for world in find_all("World", exact=True):
            try:
                if _is_rooted(world):
                    name = str(world.Outer.Name) if world.Outer else str(world.Name)
                    rooted.append(f"world {name} {int(world.ObjectFlags):#x}")
            except Exception:  # noqa: BLE001
                continue
        for holder in find_all(_HOLDER, exact=False):
            try:
                if _is_rooted(holder):
                    rooted.append(f"{holder.Name} {int(holder.ObjectFlags):#x}")
            except Exception:  # noqa: BLE001
                continue
        logging.info(
            f"[{TITLE}] leaving {_map_now(pc)}, rooted right now:"
            f" {', '.join(rooted) if rooted else 'nothing'}"
        )
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[{TITLE}] travel census failed: {e!r}")


@hook("WillowGame.WillowPlayerController:WillowClientShowLoadingMovie")
def _loading_started(
    _obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    # fires on every machine the moment a loading screen comes up, host and client both, for any
    # kind of travel. on the host it also fires once per remote player, the latch in the census
    # makes that free. this used to run the 2.10.0 sweep, now it only writes the log line
    global _settle_collect_at
    # arm the tidy up here too, the arrival hook misses some loads and then it never fires. safe
    # while a loads still going, it waits for calm play anyway
    if unstick_reloads.value:
        _settle_collect_at = time.monotonic() + _SETTLE_COLLECT_DELAY
    try:
        _travel_census(_obj)
    except Exception as e:  # noqa: BLE001
        logging.error(f"[{TITLE}] travel census hook failed: {e!r}")


def _wedge_attempt(pc: Any) -> None:
    """one attempt at unsticking a wedged game. the cut and the unroot used to be ten seconds
    apart, gentlest first, but the cut alone never rescues anything so its all one go now and the
    escalation is just asking for the collect louder.
    """
    global _wedge_stage, _wedge_next_attempt_at
    _wedge_stage += 1
    _wedge_next_attempt_at = time.monotonic() + _WEDGE_ESCALATE_AFTER

    logging.warning(
        f"[{TITLE}] the game is wedged at {_wedge_fps:.1f} fps on {_map_now(pc)},"
        f" unstick attempt {_wedge_stage} of {_WEDGE_LAST_STAGE}"
    )

    if _wedge_stage == 1:
        # the game is drawing the odd frame so this has a real chance of landing on screen, and
        # if it doesnt, _say already falls back to the log
        _say("Game froze, unsticking it, hang on.", 10.0)
        cut = _cut_cross_level_refs()
        freed = _unroot_stale_worlds(pc)
        serials = _unroot_serializers()
        _ask_for_collect(pc)
        logging.info(
            f"[{TITLE}] cut {cut} reference(s), unrooted {freed} world(s) and"
            f" {serials} serializer(s), now the collect can actually finish"
        )
    else:
        # still on the floor so the polite ask didnt land. ask again and go through the console
        # too, obj garbage is the same collect by the back door. worst case the engines own
        # schedule still gets there, it just takes a couple of minutes
        _ask_for_collect(pc)
        try:
            pc.ConsoleCommand("obj garbage")
            logging.info(f"[{TITLE}] told the console to collect too")
        except Exception as e:  # noqa: BLE001
            logging.warning(f"[{TITLE}] console collect failed: {e!r}")


def _wedge_watch() -> None:
    """one frame of watching for the wedge. counting the calls IS the framerate, one call is one
    drawn frame, and the viewport not ticking during a load is exactly what makes the stall
    visible from in here."""
    global _wedge_last_sample, _wedge_ticks, _wedge_fps
    global _wedge_saw_stall, _wedge_stall_gap, _wedge_low_streak, _wedge_stage
    global _wedge_next_attempt_at, _settle_collect_at

    now = time.monotonic()

    # the first frame has nothing to measure against, monotonic counts from boot not from launch,
    # so the first gap would read as a stall of hours
    if _wedge_last_sample <= 0.0:
        _wedge_last_sample = now
        _wedge_ticks = 0
        return

    gap = now - _wedge_last_sample
    if gap < 1.0:
        return
    _wedge_last_sample = now
    _wedge_fps = _wedge_ticks / gap
    _wedge_ticks = 0

    # we wanted a sample every second and didnt get one for ten, so no frames were drawn at all
    # in between. thats a freeze, not slowness, and its the first half of the trigger
    if gap >= _WEDGE_BIG_STALL:
        _wedge_saw_stall = True
        _wedge_stall_gap = gap
        logging.warning(f"[{TITLE}] no frames at all for {gap:.1f}s")

    _wedge_low_streak = _wedge_low_streak + 1 if _wedge_fps < _WEDGE_LOW_FPS else 0

    # the calm play collect, armed by every arrival. only a visibly fine game gets asked, one
    # thats crawling or mid rescue gets waited on, and one that never calms down gets left alone
    if _settle_collect_at and unstick_reloads.value:
        if now >= _settle_collect_at + _SETTLE_GIVE_UP:
            _settle_collect_at = 0.0
        elif (
            now >= _settle_collect_at
            and not _wedge_saw_stall
            and _wedge_stage == 0
            and _wedge_fps >= _SETTLE_CALM_FPS
        ):
            _settle_collect_at = 0.0
            logging.info(
                f"[{TITLE}] game is calm at {_wedge_fps:.0f} fps, asking for the tidy up"
                " collect so the next load has nothing to trip on"
            )
            _ask_for_collect(get_pc(possibly_loading=True))

    if _wedge_fps >= _WEDGE_RECOVERED_FPS:
        if _wedge_stage:
            # the line this whole investigation was for
            logging.info(
                f"[{TITLE}] RECOVERED after attempt {_wedge_stage},"
                f" back to {_wedge_fps:.1f} fps"
            )
            _say("Unstuck, carry on.")
        if _wedge_stage or _wedge_saw_stall:
            # either we won or it picked itself up, a long load on a tired drive can freeze for
            # ten seconds and then be fine. clean slate either way, an old stall must never pair
            # up with ordinary combat lag an hour later and fire this against a live map
            _wedge_stage = 0
            _wedge_saw_stall = False
            _wedge_stall_gap = 0.0
            _wedge_next_attempt_at = 0.0
        return

    # the trigger, all of it or nothing happens. froze outright, still on the floor seconds later,
    # and enough time since the last attempt that a fix that worked would have shown. a twenty
    # second freeze has no innocent explanation so that one gets convicted quicker
    streak_needed = (
        _WEDGE_SURE_STREAK if _wedge_stall_gap >= _WEDGE_SURE_STALL else _WEDGE_LOW_STREAK_NEEDED
    )
    if (
        unstick_reloads.value
        and _wedge_saw_stall
        and _wedge_low_streak >= streak_needed
        and _wedge_stage < _WEDGE_LAST_STAGE
        and now >= _wedge_next_attempt_at
    ):
        _wedge_attempt(get_pc(possibly_loading=True))


@hook("WillowGame.WillowGameViewportClient:Tick")
def _on_tick(
    _obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    # this runs every frame, several hundred times a second. one NameError in here with no
    # handling wrote a quarter of a million log lines in two minutes and jammed the mod, the
    # exception fired before the pending flag could be cleared
    global _tick_errors, _tick_errors_total, _pending_reload, _hold_until, _hold_watching
    global _travel_station, _client_rollback_at
    global _wedge_ticks, _wedge_errors, _wedge_dead

    # one call is one drawn frame, counting them is the only honest framerate from in here. the
    # watch cant live in _tick_body, that bails when no reload of ours is on and wedges happen on
    # plain loads too, and it gets its own handling so it cant abandon someone elses reload
    _wedge_ticks += 1
    if not _wedge_dead:
        try:
            _wedge_watch()
            _wedge_errors = 0
        except Exception as e:  # noqa: BLE001
            _wedge_errors += 1
            if _wedge_errors >= 5:
                _wedge_dead = True
                logging.error(
                    f"[{TITLE}] wedge watch kept failing, its off until next launch: {e!r}"
                )
            elif _wedge_errors == 1:
                logging.error(f"[{TITLE}] wedge watch failed: {e!r}")

    try:
        _tick_body()
        # a clean frame resets the count, five means five in a row, not five spread out over a
        # whole evening of otherwise fine reloads
        _tick_errors = 0
    except Exception as e:  # noqa: BLE001
        _tick_errors += 1
        _tick_errors_total += 1
        # log the first few then back right off, do not carpet the log
        if _tick_errors_total in (1, 5, 50) or _tick_errors_total % 500 == 0:
            logging.error(f"[{TITLE}] tick failed ({_tick_errors_total}x): {e!r}")
        if _tick_errors >= 5:
            # unstick ourselves rather than retry forever. always say so, this happens at most
            # once per incident and silence here cost a whole debugging session once
            logging.error(f"[{TITLE}] too many tick failures in a row, abandoning this reload")
            _pending_reload = False
            _hold_until = 0.0
            _hold_watching = False
            _travel_station = None
            _client_rollback_at = 0.0
            _remote_stand_down("tick kept failing")


def _tick_body() -> None:
    # per frame, so bail instantly when theres nothing on. AsyncUtil hooks this same function so
    # the path is real, and the one time log below proves its actually running.
    global _pending_reload, _travel_from_map, _map_stable_since
    global _hold_until, _hold_logged, _hold_on_target_since, _hold_last_apply, _hold_prev
    global _hold_reached, _hold_watching, _tick_alive_logged, _map_is_up, _travel_station
    global _gc_grace_ticks, _saved_location, _rolled_back
    global _remote_until, _client_rollback_at, _client_rollback_tries

    if not _tick_alive_logged:
        _tick_alive_logged = True
        logging.info(f"[{TITLE}] tick hook alive")

    if not _pending_reload and not _hold_until and not _remote_until and not _client_rollback_at:
        return

    # every branch below needs this. it went missing in a rewrite once and the whole tick threw a
    # NameError every frame, which quietly killed the restore, the restock and the revive at once
    now = time.monotonic()

    pc = get_pc(possibly_loading=True)
    if pc is None:
        return

    # our own missions, on our own machine, after the host reloaded under us. nothing else in this
    # function applies on a client so it goes first and on its own
    if _client_rollback_at and now >= _client_rollback_at:
        _client_rollback_at = 0.0
        # the one second wait was a guess at how long the servers state takes to arrive. if its
        # still not here, wait another second and ask again, up to five, because a machine having
        # a bad time loading is exactly the machine this matters on
        if not _rollback_missions(pc):
            _client_rollback_tries += 1
            if _client_rollback_tries < 5:
                _client_rollback_at = now + 1.0
            else:
                logging.warning(
                    f"[{TITLE}] gave up waiting for the mission tracker after"
                    f" {_client_rollback_tries} tries, missions stay as they are"
                )

    # a parked travel goes first, it exists so the collect scheduled at the keypress gets a
    # world tick to actually run before the transition starts
    if _pending_reload and _travel_station is not None:
        _gc_grace_ticks -= 1
        if _gc_grace_ticks <= 0:
            station = _travel_station
            _travel_station = None
            try:
                logging.info(f"[{TITLE}] travelling now")
                pc.WorldInfo.Game.TravelToStation(station, True)
            except Exception as e:  # noqa: BLE001
                logging.error(f"[{TITLE}] deferred travel failed: {e!r}")
                _pending_reload = False
        return

    if _pending_reload:
        # deadline first, before the pawn gate. with the pawn check first a travel that never
        # landed stayed armed forever and then fired against whatever map came up next, even a
        # different character hours later
        if now - _travel_started_at >= 60.0:
            _pending_reload = False
            _map_is_up = False
            _rolled_back = False
            _saved_location = None
            _saved_positions.clear()
            _remote_stand_down("travel never settled")
            logging.warning(f"[{TITLE}] travel never settled, standing down")
            return

        if pc.Pawn is None:
            return

        # the loading movie ending is the real signal. the map name is useless here because a
        # reload of the same map never changes it.
        if not _map_is_up:
            return

        # let it settle for a moment after the screen clears
        if now - _map_stable_since < 0.5:
            return

        _pending_reload = False
        _map_is_up = False
        here = _map_now(pc)
        logging.info(
            f"[{TITLE}] new map up after {now - _travel_started_at:.1f}s"
            f" ({_travel_from_map} -> {here})"
        )

        _arm_population_reset()

        # who is still here. if this drops from 2 to 1 we have caught the partner being booted,
        # with a timestamp, instead of relying on someone noticing and telling us later.
        try:
            still_here = [str(pri.PlayerName) for pri in _players(pc)]
            if len(still_here) < _players_before[0]:
                logging.error(
                    f"[{TITLE}] LOST A PLAYER during travel:"
                    f" {_players_before[1]} -> {still_here}"
                )
            else:
                logging.info(f"[{TITLE}] still here after travel: {still_here}")
        except Exception:  # noqa: BLE001
            pass

        _restock_everyone(pc)

        # second pass, on the tracker this map just built. everything written before the travel
        # gets rebuilt over, so the pre travel pass can look perfect in the log and the quest
        # still never comes back. if this pass rolls the same mission again, thats why
        if _rolled_back:
            _rolled_back = False
            # this block only ever runs once per reload, so if the tracker isnt up yet theres no
            # second chance here. hand it to the timer above instead, thats what it does
            if not _rollback_missions(pc):
                _client_rollback_at = now + 1.0
                _client_rollback_tries = 0

        # no collect here any more. the load just ran a full transition gc of its own, and the
        # next press collects again before travelling, doing one now only hitches the first
        # frames after control comes back

        if here != _travel_from_map:
            # we landed somewhere other than the map the spots were noted in, those coordinates
            # mean nothing here, do not shove anyone into them
            if _saved_location is not None:
                logging.info(f"[{TITLE}] different map, skipping the position restore")
            _saved_location = None
            _saved_positions.clear()

        # arm the partner watcher above the bail below, our own restore having nothing to do says
        # nothing about theirs
        _arm_remote_watch(pc, now)

        if _saved_location is None:
            _say("Map reloaded.")
            return

        _hold_until = now + _HOLD_MAX_SECONDS
        _hold_logged = False
        _hold_on_target_since = 0.0
        _hold_last_apply = 0.0
        _hold_prev = None
        _hold_reached = False
        _hold_watching = False
        _say("Map reloaded, lobby intact.")

    # before the hold, not after it. the hold block is all early returns, so a watcher under it
    # only gets a frame once our own restore is finished, which is the window theirs matters in
    if _remote_until:
        _remote_tick(pc, now)

    if _hold_until:
        if pc.Pawn is None or _saved_location is None:
            if now >= _hold_until:
                _hold_until = 0.0
                _hold_watching = False
            return

        # measure BEFORE we write anything this frame. reading it after the apply just reads our
        # own write back, which always matches, thats the bug that made the old hold pass in 8ms
        loc = pc.Pawn.Location
        pos = (loc.X, loc.Y, loc.Z)
        prev = _hold_prev
        _hold_prev = pos
        jumped = prev is not None and any(
            abs(pos[i] - prev[i]) > _HOLD_JUMP for i in range(3)
        )

        if _hold_watching:
            # the hold finished, this is the lookout for the engines late placement. nobody covers
            # 250 units in one frame so a jump here is the engine, and we take the spot from just
            # before it, that way someone who walked off after settling keeps their walk
            if now >= _hold_until:
                _hold_until = 0.0
                _hold_watching = False
                return
            if jumped:
                logging.info(
                    f"[{TITLE}] late teleport to ({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}),"
                    " grabbing you back"
                )
                _saved_location = prev
                _hold_watching = False
                _hold_on_target_since = 0.0
                _hold_reached = False
                _apply_my_position(pc)
                _hold_last_apply = now
            return

        on_target = all(abs(pos[i] - _saved_location[i]) < _HOLD_TOLERANCE for i in range(3))

        if on_target:
            _hold_reached = True
            if not _hold_on_target_since:
                _hold_on_target_since = now
        else:
            _hold_on_target_since = 0.0

        settled = bool(_hold_on_target_since) and (
            now - _hold_on_target_since >= _HOLD_SETTLED_SECONDS
        )
        # once we know the spot stuck at least once, drifting off it without a teleport is just
        # the player walking, let them
        walked_off = _hold_reached and not on_target and not jumped
        timed_out = now >= _hold_until

        if settled or walked_off or timed_out:
            # no correction for anyone else here any more. our hold finishing means nothing to
            # them, they get their own watcher on their own clock
            reason = "settled" if settled else ("player moved off" if walked_off else "timed out")
            logging.info(
                f"[{TITLE}] hold finished ({reason})"
                f" at ({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f})"
            )
            if timed_out:
                _hold_until = 0.0
                _hold_prev = None
            else:
                # dont stand down all the way, keep the lookout up until the deadline
                _hold_watching = True
            return

        first = not _hold_logged
        if first or (not on_target and now - _hold_last_apply >= _HOLD_APPLY_INTERVAL):
            _apply_my_position(pc)
            _hold_last_apply = now
            if first:
                _hold_logged = True
                logging.info(
                    f"[{TITLE}] holding position, target"
                    f" ({_saved_location[0]:.0f}, {_saved_location[1]:.0f},"
                    f" {_saved_location[2]:.0f})"
                )


def _on_enable() -> None:
    # arm straight away so it also covers a normal fast travel or walking between maps, not just
    # our own key
    lootables, enemies = _arm_population_reset()
    logging.info(f"[{TITLE}] enabled, armed {lootables} lootable + {enemies} enemy population defs")


def _on_disable() -> None:
    # toggling the mod off mid reload must not leave the state machine armed, itd fire against
    # whatever map comes up after a re-enable
    global _pending_reload, _hold_until, _hold_watching, _map_is_up, _saved_location
    global _travel_station, _rolled_back, _client_rollback_at, _client_rollback_tries
    global _wedge_last_sample, _wedge_ticks, _wedge_saw_stall, _wedge_stall_gap
    global _wedge_low_streak, _wedge_stage, _wedge_next_attempt_at, _settle_collect_at
    _pending_reload = False
    _hold_until = 0.0
    _hold_watching = False
    _map_is_up = False
    _rolled_back = False
    _saved_location = None
    _travel_station = None
    _client_rollback_at = 0.0
    _client_rollback_tries = 0
    _saved_positions.clear()
    # the wedge watch too, otherwise a re-enable measures the whole time the mod was off as one
    # giant stall and half arms the trigger against a perfectly healthy game
    _wedge_last_sample = 0.0
    _wedge_ticks = 0
    _wedge_saw_stall = False
    _wedge_stall_gap = 0.0
    _wedge_low_streak = 0
    _wedge_stage = 0
    _wedge_next_attempt_at = 0.0
    _settle_collect_at = 0.0
    _remote_stand_down("mod turned off")


build_mod(
    name=TITLE,
    author="web",
    version="2.10.3",
    description=(
        "Reload the map for fresh loot without kicking your co-op partner. Uses the game's own"
        " travel instead of a save quit, and arms the population reset flag so chests, vendors and"
        " enemies actually refresh. Only the host presses the key. Read only is the exception -"
        " saving happens on each players own machine, so anyone who wants their own progress held"
        " back needs this installed too, with Save Before Reloading turned off."
    ),
    mod_type=ModType.Standard,
    supported_games=Game.BL2 | Game.TPS | Game.AoDK,
    coop_support=CoopSupport.HostOnly,
    on_enable=_on_enable,
    on_disable=_on_disable,
)
