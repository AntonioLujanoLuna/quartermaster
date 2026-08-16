// The few things both the screens and the action results have to say the same
// way. Coin is the whole list so far, and it is here rather than in either of
// them because a purse rendered one way and a receipt rendered another is two
// chances to disagree about the same balance.

// The denominations a table sees. Electrum is off unless a campaign turns it
// on, and the server is where that decision lives, so it appears only when a
// balance actually carries some.
const VISIBLE = ["cp", "sp", "gp", "pp"];

export function formatCoin(balance) {
  const denominations = Number(balance?.ep) ? ["cp", "sp", "ep", "gp", "pp"] : VISIBLE;
  return denominations.map((denomination) => `${Number(balance?.[denomination]) || 0} ${denomination}`).join(" · ");
}

export function hasCoin(balance) {
  return Object.values(balance || {}).some((amount) => Number(amount) > 0);
}
