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
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (error.code === "HANDLE") {
        notify("bad", "That control had already been used. Nothing happened — try again.");
        await reload();
        return;
      }
      notify("bad", error.message);
      // A refusal is often a refusal *about* state that moved, so the screen is
      // read again rather than left showing what produced it.
      await reload();
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
        text: question(error.message),
        confirmLabel,
        // A second decision by the player, so it is a second action and gets
        // its own key.
        run: () => perform(async () => report(await confirm(actionKey()))),
      });
      return null;
    }
  }

  const tookIt = (answer) => {
    const it = answer.result;
    return `You took ${it.quantity} ${it.item_name}. ${it.remaining} remain in the Party Stash.`;
  };

  const gaveIt = (answer) => {
    const it = answer.result;
    return `${it.character_name} gave ${it.quantity} ${it.item_name} to ${it.destination_name}. ${it.remaining} still held.`;
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
  };
}
