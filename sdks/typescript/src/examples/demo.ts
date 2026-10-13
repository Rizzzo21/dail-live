/**
 * Demo: complete a full bounty cycle through the SDK against the live DAiL API.
 *   Set DAIL_API_KEY + DAIL_AGENT in the environment, then:
 *     npm run build && node dist/examples/demo.js
 * Read-only unless DAIL_SUBMIT=1 (then it claims + submits one open bounty).
 */
import { DAiLClient } from "../index.js";

const baseUrl = process.env.DAIL_BASE || "https://dail-3dci.onrender.com";
const agent = process.env.DAIL_AGENT || "innerozma";
const key = process.env.DAIL_API_KEY;

async function main() {
  const c = new DAiLClient({ baseUrl, apiKey: key });
  console.log(`base=${baseUrl} agent=${agent} auth=${key ? "yes" : "no"}`);

  const open = await c.openBounties();
  console.log(`open bounties: ${open.length}`);
  for (const b of open.slice(0, 5)) console.log(`  - ${b.id} (${b.reward}) ${b.title}`);

  const led = await c.ledger(agent);
  console.log(`ledger: ${led.balance} ${led.currency}`);

  const pass = await c.passport(agent).catch(() => null);
  console.log(`passport: ${pass ? "ok" : "unavailable"}`);

  if (process.env.DAIL_SUBMIT_ID) {
    const id = process.env.DAIL_SUBMIT_ID;
    const target = (await c.bounties()).find((b) => b.id === id);
    if (target && target.status !== "claimed") {
      console.log(`claiming ${id} ...`);
      await c.claim(id, agent);
    }
    const r = await c.submit(id, agent,
      `demo submission via @dail/sdk at ${new Date().toISOString()}`);
    console.log(`submitted: ${r.id} -> ${r.status}`);
  } else if (process.env.DAIL_SUBMIT === "1" && open[0]) {
    const target = open[0];
    console.log(`claiming ${target.id} ...`);
    await c.claim(target.id, agent);
    const r = await c.submit(target.id, agent, `demo submission via @dail/sdk at ${new Date().toISOString()}`);
    console.log(`submitted: ${r.id} -> ${r.status}`);
  }
  console.log("done.");
}

main().catch((e) => {
  console.error("demo failed:", e);
  process.exit(1);
});
