// The clarify prompt renders with the agent's authority, so a question asking
// for a password is the one thing it must refuse to collect. This pulls the
// real pattern out of the bundle and asks it about real questions -- asserting
// the source text contains a regex proves nothing about what it matches.

const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(
  path.join(__dirname, "..", "..", "ettok-chat", "dashboard", "dist", "index.js"), "utf8");

const start = SRC.indexOf("const SECRET_RE = new RegExp(");
const end = SRC.indexOf('"i");', start);
if (start === -1 || end === -1) {
  console.error("FAIL: SECRET_RE is gone from the bundle — the clarify prompt is unguarded");
  process.exit(1);
}
const SECRET_RE = eval("(" + SRC.slice(start + "const SECRET_RE = ".length, end + 5).replace(/;$/, "") + ")");

// Questions the agent is forbidden to ask. One arriving is evidence that
// something it read talked it into asking.
const BLOCK = [
  "What is your Facebook password?",
  "Paste the API key for DeepSeek",
  "Enter your passphrase",
  "Send me the access token",
  "What is the CVV on the card?",
  "What is the one time code?",
  "Your seed phrase, please",
  "I need the login details",
  "Share your credentials",
  "Enter the 2FA code",
  "What is the card number?",
  "what's the pass word",
];

// Ordinary monitoring questions. Blocking these would train people to dismiss
// the banner, which is worse than not having it.
const ALLOW = [
  "Which community should I prioritise?",
  "Should I escalate this case?",
  "Is this account key to the network?",
  "Pick a keyword to add",
  "Which Telegram channel should I watch?",
  "Should I dismiss this report as counter-speech?",
  "Which of these is the survivor's own testimony?",
];

const failures = [];
for (const q of BLOCK) if (!SECRET_RE.test(q)) failures.push("let through: " + q);
for (const q of ALLOW) if (SECRET_RE.test(q)) failures.push("false positive: " + q);

if (failures.length) {
  console.error("FAIL\n" + failures.join("\n"));
  process.exit(1);
}
console.log("ok — " + BLOCK.length + " credential questions blocked, "
            + ALLOW.length + " ordinary ones let through");
