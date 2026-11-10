// The JS side of scumm2escoria's patches to Escoria's addons. The engine
// (godot-escoria-js-engine) runs Escoria as it comes; the converter changes a
// few of Escoria's functions, so the game brings those changes here, through
// the engine's hooks named after the function they change.
//
// `patches` lists every function of Escoria the converter changed
// ("<addon script>:<function>"): tools/gd2js/translate.js compares the game's
// addons with Escoria's and warns about any change not listed here.

const CORE = "res://addons/escoria-core/game/core-scripts/";
const UI = "res://addons/escoria-ui-keyboard-9verbs/";

export const patches = [
  // with a JS side (install below)
  `${CORE}esc/esc_object_manager.gd:has`,
  `${CORE}esc/esc_action_manager.gd:_get_event_to_queue`,
  `${CORE}esc_animation_player.gd:_on_animation_finished`,
  `${UI}game.gd:left_click_on_inventory_item`,
  `${CORE}esc/esc_event_manager.gd:interrupt`,
  // v5 games, nothing to do in JS: the engine's `while` already stops when its
  // event is interrupted, an assignment isn't taken for a return code, and it
  // has no static analysis of :exit_scene
  `${CORE}esc/compiler/esc_interpreter.gd:visit_while_stmt`,
  `${CORE}esc/compiler/esc_interpreter.gd:visit_expression_stmt`,
  "res://addons/escoria-core/tools/script_analysis/analyzers/esc_script_analyzer.gd:_look_up_variable",
  // Godot 4 fixes, nothing to do in JS: the camera's `current` (pause menu),
  // EditorInterface in exported games, Godot 3's set_loop() in the speech
  // player, verbs_menu.gd skipping children that aren't buttons (the engine's
  // verbs menu only takes buttons)
  `${UI}game.gd:pause_game`,
  `${UI}game.gd:unpause_game`,
  `${CORE}esc_location.gd:_ready`,
  `${CORE}esc_location.gd:_exit_tree`,
  `${CORE}esc_location.gd:_is_gizmo_visible`,
  "res://addons/escoria-core/game/scenes/sound/esc_speech_player.gd:set_state",
  `${UI}verbs_menu.gd:_ready`,
  `${UI}verbs_menu.gd:on_action_selected`,
  `${UI}verbs_menu.gd:unselect_actions`,
  `${UI}verbs_menu.gd:set_by_name`,
];

/**
 * @param cfg config.js: cfg.patches = {inventoryVerb, pickup} (what the
 *   converter patched for this game)
 */
export function install(esc, api, cfg = {}) {
  const p = cfg.patches || {};

  // esc_object_manager.gd has(): like get_object(), inventory items registered
  // to another room count
  esc.hooks.objectManagerHas = (id) => esc.inventory.has(id) && !!esc.inventoryObject(id);

  // esc_action_manager.gd _get_event_to_queue(): a combination nothing handles
  // runs the item's "<verb>_with", then the default script's, before its "<verb>"
  esc.hooks.combinationFallback = (target, action, _combineWith, { hasEvent, getEvent, defaults }) => {
    if (hasEvent(target, action + "_with")) return getEvent(target, action + "_with");
    if (defaults && hasEvent(defaults, action + "_with")) return getEvent(defaults, action + "_with");
    return null;
  };

  // esc_action_manager.gd _get_event_to_queue(): ScummDefaults.publish(target,
  // combine_with) before an action (ui/scumm_defaults.gd, translated by
  // tools/gd2js)
  const gd = api.bridge ? api.bridge(esc) : null;
  esc.actions.beforeEvent = (a, b) => {
    const D = gd && gd.classOrNull("ScummDefaults");
    if (D) D.publish(gd.objectWrapper(a), gd.objectWrapper(b));
  };

  // 9-verbs game.gd left_click_on_inventory_item(): no verb (or Pick up) uses
  // the item's inventory verb, as SCUMM does
  if (p.inventoryVerb) {
    esc.hooks.inventoryLeftClick = (obj, am) => {
      const verb = obj.node.props.default_action_inventory;
      if (verb && ["", "walk", p.pickup || "pickup"].includes(am.currentAction)) am.setCurrentAction(verb);
    };
  }

  // esc_event_manager.gd interrupt(): a room's idle animations and routines live
  // on channels of their own, and only a change of scene ends them
  if (p.backgroundChannels) {
    esc.hooks.keepsChannel = (channel, changingScene) => !changingScene && channel.startsWith("scumm_bg");
  }

  // esc_animation_player.gd _on_animation_finished(): a non-looping
  // AnimatedSprite2D pauses on its last frame (stop() would rewind it)
  api.AnimPlayer.stopPauses = true;

  // not a patch: the commands that only set state (the viewer runs them from
  // :init when it opens a room on its own)
  for (const c of ["scumm_set_name", "scumm_state_var", "scumm_screen_effect"]) esc.stateCommands.add(c);
}
