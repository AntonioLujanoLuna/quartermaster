// Stage 4 of docs/activity-migration-plan.md: what a player can do here, and
// what happens when the domain says no.
//
// Every action in this module has the same three parts. It prepares, if the
// mutation is addressed by a handle. It mints one key for the action and
// carries that key through every request the action makes, so a retry over a
// bad connection is a retry rather than a second take. And it turns the
// server's answer into a sentence, because the panels always answered in
// sentences and a screen that reports `{"status": "TAKEN"}` is a step back.
//
// The refusals are the reason this is a module rather than a handful of click
// handlers. Three of them mean different things to a player:
//
//   STALE    the number moved between deciding and pressing. This is a
//            question, and it is put to the player rather than resolved for
//            them — answering it is what the confirm routes are for.
//   HANDLE   the control was already spent, or it expired. Nothing happened,
//            and the screen is re-read so the next press is against what is
//            actually there.
//   REFUSED  the domain's answer, and the end of it. It is already a sentence.
//
// Nothing here sends an actor id. The server reads that from the token, and a
// client that offered one would be ignored.

import { api, ApiError, actionKey } from "./api.js";
import { formatCoin } from "./format.js";

/**
 * Build the action layer over the three things it needs from the screen.
 *
 * `notify` says what happened, `ask` puts a question and runs the answer, and
 * `reload` re-reads what is on screen. Nothing here touches the DOM.
 */
export function createActions({ notify, ask, reload }) {
  /**
   * Run one action and report it, whatever it turns out to be.
   *
   * The reload on success is deliberate and is not the live feed doing its
   * job: the feed exists to tell you about everyone else's changes, and your
   * own should not wait for a round trip through it — least of all when the
   * socket is the thing that is down.
   */
  async function perform(work) {
    try {
      const said = await work();
      if (said) notify("ok", said);
      await reload();
      // Answered rather than refused, which is what the forms need to know
      // before they empty themselves: losing what was typed is only acceptable
      // once the thing it described exists.
      return true;
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (error.code === "HANDLE") {
        notify("bad", "That control had already been used. Nothing happened — try again.");
        await reload();
        return false;
      }
      notify("bad", error.message);
      // A refusal is often a refusal *about* state that moved, so the screen is
      // read again rather than left showing what produced it.
      await reload();
      return false;
    }
  }

  /** A mutation that may come back as a question, and the answer if it does. */
  async function withConfirmation({ attempt, confirm, question, confirmLabel, report }) {
    const key = actionKey();
    try {
      return report(await attempt(key));
    } catch (error) {
      if (!(error instanceof ApiError) || error.code !== "STALE") throw error;
      ask({
        // Awaited, because one of these questions has to go and read the
        // state that changed before it can ask about it.
        text: await question(error.message),
        confirmLabel,
        // A second decision by the player, so it is a second action and gets
        // its own key.
        run: () => perform(async () => report(await confirm(actionKey()))),
      });
      return null;
    }
  }

  /**
   * Commit a previewed split, and ask again if the roster moved under it.
   *
   * The second preview exists only to be read. What commits is the original
   * handle with `confirm_current`, because that is what says the DM saw the
   * new numbers and meant them; the handle the second preview minted expires
   * unspent, which is what asking the question costs.
   */
  function commitSplit(handleId, amounts) {
    return perform(() =>
      withConfirmation({
        attempt: (key) => api.splitTreasury(handleId, false, key),
        confirm: (key) => api.splitTreasury(handleId, true, key),
        question: async (detail) => {
          const now = await api.prepareSplit(amounts);
          return `${detail}. ${describeSplit(now)} Split it that way?`;
        },
        confirmLabel: "Split it now",
        report: (answer) => {
          const it = answer.result;
          const count = it.recipients.length;
          return `Split ${formatCoin(it.per_recipient)} to each of ${count} ${
            count === 1 ? "character" : "characters"
          }.`;
        },
      }),
    );
  }

  const tookIt = (answer) => {
    const it = answer.result;
    return `You took ${it.quantity} ${it.item_name}. ${it.remaining} remain in the Party Stash.`;
  };

  const gaveIt = (answer) => {
    const it = answer.result;
    return `${it.character_name} gave ${it.quantity} ${it.item_name} to ${it.destination_name}. ${it.remaining} still held.`;
  };

  const describeRoll = (result) => {
    const mode = result.mode === "normal" ? "" : ` with ${result.mode}`;
    const label = result.label || result.expression;
    const recorded = result.recorded === false ? " It was not added to the session log." : "";
    return `${label}: ${result.total}${mode}.${recorded}`;
  };

  return {
    async take(item, amount) {
      await perform(async () => {
        const { handle_id: handle } = await api.prepareTake(item.id, amount);
        return withConfirmation({
          attempt: (key) => api.take(handle, key),
          confirm: (key) => api.confirmTake(handle, key),
          question: (detail) => `${detail}. Take the quantity that is there now?`,
          confirmLabel: "Take the current quantity",
          report: tookIt,
        });
      });
    },

    async give(item, destination, amount) {
      await perform(async () => {
        const prepared = await api.prepareGive(item.id);
        const handle = amount === "all" ? prepared.handles.all : prepared.handles.one;
        return withConfirmation({
          attempt: (key) => api.give(handle, destination, key),
          confirm: (key) => api.confirmGive(handle, destination, key),
          question: (detail) => `${detail}. Give the quantity that is there now?`,
          confirmLabel: "Give the current quantity",
          report: gaveIt,
        });
      });
    },

    /** A quantity the player typed, which has nothing on screen to go stale. */
    async giveQuantity(item, destination, quantity) {
      await perform(async () =>
        gaveIt(await api.giveSome(item.item_name, quantity, destination, actionKey())),
      );
    },

    async use(item, quantity, reason) {
      await perform(async () => {
        const it = (await api.use(item.id, quantity, reason || null, actionKey())).result;
        return `You used ${it.quantity} ${it.item_name}. ${it.remaining} still held.`;
      });
    },

    async rollDice(expression, mode, label, visibility) {
      let result = null;
      await perform(async () => {
        const answer = await api.rollDice(expression, mode, label || null, visibility, actionKey());
        result = answer.result;
        return describeRoll(result);
      });
      return result;
    },

    async claim(dropItem, amount) {
      await perform(async () => {
        const it = (await api.claim(dropItem.id, amount, actionKey())).result;
        if (it.status !== "CLAIMED") {
          return "That Loot Drop is no longer open. What was left went back to the Party Stash.";
        }
        return `You claimed ${it.quantity} ${it.item_name}. ${it.remaining} remain in the drop.`;
      });
    },

    async returnCoin(amounts, destination) {
      await perform(async () => {
        const it = (await api.returnCoin(amounts, destination, actionKey())).result;
        return `${it.character_name} gave ${formatCoin(it.amount)} to ${it.destination_name}. Still carrying ${formatCoin(it.character_after)}.`;
      });
    },

    async giveCoin(character, amounts) {
      await perform(async () => {
        const it = (await api.giveCoin(character.id, amounts, actionKey())).result;
        return `Gave ${formatCoin(it.amount)} to ${it.character_name}.`;
      });
    },

    async registerCharacter(name, discordUserId) {
      await perform(async () => {
        const it = (await api.registerCharacter(name, discordUserId, actionKey())).result;
        return `Registered ${it.name}.`;
      });
    },

    // Stage 5 -----------------------------------------------------------------
    //
    // What the DM does. Every one of these is refused by the API for anyone
    // whose token does not say DM, so the screen not rendering them is a
    // courtesy rather than the check.

    async grant(itemName, quantity, provenance) {
      return perform(async () => {
        const it = (await api.grant(itemName, quantity, provenance || null, actionKey())).result;
        return `Granted ${it.quantity} ${it.item_name}. The Party Stash holds ${it.new_quantity}.`;
      });
    },

    async correct(item, quantity, reason) {
      await perform(async () => {
        const it = (await api.correct(item.id, quantity, reason || null, actionKey())).result;
        return `Removed ${it.quantity} ${it.item_name} from the Party Stash. ${it.remaining} remain.`;
      });
    },

    async createDrop(items, expiryHours) {
      return perform(async () => {
        const it = (await api.createDrop(items, expiryHours, actionKey())).result;
        const count = it.items.length;
        return `Loot Drop open with ${count} ${count === 1 ? "item" : "items"}, until ${it.expires_at}.`;
      });
    },

    async closeDrop(dropId) {
      await perform(async () => {
        const it = (await api.closeDrop(dropId, actionKey())).result;
        const returned = it.returned_item_count ?? 0;
        return returned
          ? `Drop closed. ${returned} unclaimed went back to the Party Stash.`
          : "Drop closed. Nothing was left in it.";
      });
    },

    async adjustTreasury(deltas, reason) {
      await perform(async () => {
        const it = (await api.adjustTreasury(deltas, reason || null, actionKey())).result;
        return `Treasury is now ${formatCoin(it.after)}.`;
      });
    },

    /**
     * Split the treasury, which is two decisions and sometimes three.
     *
     * Pressing Split does not split anything. The share each character gets
     * depends on how many are alive, and the DM cannot see the roster from
     * inside a form — so this prepares the split, names who is being paid and
     * what each of them gets, and waits. That is the panel's shape, and it is
     * the shape because submitting used to be the split and a death in between
     * silently changed everyone's share.
     *
     * The third decision is the unhappy path: if the roster moved between the
     * preview and the press, the handle no longer matches what was shown and
     * the commit refuses. The question is then asked again with the shares as
     * they now stand.
     */
    async split(amounts) {
      await perform(async () => {
        const preview = await api.prepareSplit(amounts);
        ask({
          text: `Split ${formatCoin(amounts)}? ${describeSplit(preview)} Nothing has moved yet.`,
          confirmLabel: "Split the treasury",
          run: () => commitSplit(preview.handle_id, amounts),
        });
        return null;
      });
    },

    async startSession() {
      await perform(async () => {
        const it = (await api.startSession(actionKey())).result;
        if (it.status === "ACTIVE_EXISTS") {
          // Never closed silently. Specification 28.2 makes this the DM's
          // decision, and the surface has to say so rather than resolve it.
          return `Session ${it.active_session_number} is still running. End it before starting another.`;
        }
        return `Session ${it.session_number} started.`;
      });
    },

    async importDossier(snapshot, characterName) {
      return perform(async () => {
        const it = (await api.importDossier(snapshot, actionKey())).result;
        const version = it?.snapshot_version;
        return version
          ? `Snapshot version ${version} imported for ${characterName}.`
          : `Snapshot imported for ${characterName}.`;
      });
    },

    async endSession(whereEnded, recordingUrl) {
      return perform(async () => {
        const it = (await api.endSession(whereEnded, recordingUrl, actionKey())).result;
        if (it.status === "NO_ACTIVE_SESSION") return "There is no session running.";
        const parts = [`Session ${it.session_number} ended.`];
        if (it.closed_drops) parts.push(`${it.closed_drops} open drops closed.`);
        if (it.closed_combats) parts.push(`${it.closed_combats} open fights closed.`);
        if (it.recording_url) parts.push("Recording saved.");
        return parts.join(" ");
      });
    },

    async openCombat(channelId) {
      await perform(async () => {
        const it = (await api.openCombat(channelId, actionKey())).result;
        if (it.status === "NO_ACTIVE_SESSION") return "Start a session before opening combat.";
        if (it.status === "ALREADY_OPEN") return "A fight is already open.";
        return "Combat open. Avrae still runs the fight.";
      });
    },

    async closeCombat(outcome) {
      return perform(async () => {
        const it = (await api.closeCombat(outcome || null, actionKey())).result;
        if (it.status === "NO_ACTIVE_SESSION") return "There is no session running.";
        if (it.status === "NO_OPEN_COMBAT") return "No fight was open.";
        const unclaimed = (it.open_drops || []).length;
        return unclaimed
          ? `Combat closed. ${unclaimed} Loot ${unclaimed === 1 ? "Drop is" : "Drops are"} still open.`
          : "Combat closed.";
      });
    },

    async transition(character, lifecycle) {
      await perform(async () => {
        const it = (await api.transitionCharacter(character.id, lifecycle, actionKey())).result;
        return `${it.name} is now ${it.to.toLowerCase()}. Their belongings have not moved.`;
      });
    },

    async resolveEstate(character, destination) {
      await perform(async () => {
        const it = (await api.resolveEstate(character.id, destination, actionKey())).result;
        return `Moved ${it.items_moved} stacks and ${formatCoin(it.currency_moved)} from ${
          it.source_character_name
        } to ${it.destination_name}.`;
      });
    },

    async runMaintenance() {
      await perform(async () => {
        const it = await api.runMaintenance();
        return `Expired ${it.expired_drops} drops, and cleared ${it.removed_handles} handles and ${it.removed_receipts} receipts.`;
      });
    },

    async backup() {
      await perform(async () => {
        const it = await api.backup();
        return `Snapshot written and validated: ${it.primary_path}`;
      });
    },
  };
}

/** The shares a preview describes, as the sentence a question is asked in. */
function describeSplit(preview) {
  const names = preview.recipients.map((recipient) => recipient.name).join(", ");
  const each = `${formatCoin(preview.per_recipient)} each to ${names}`;
  if (!Object.values(preview.remainder).some((value) => value > 0)) return `${each}.`;
  return `${each}, and the treasury keeps ${formatCoin(preview.remainder)} that will not divide evenly.`;
}
