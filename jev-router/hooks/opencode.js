/**
 * opencode half of the Jev router. The repository's opencode entry
 * (.opencode/plugins/agent-skills.js) loads this module and merges its hooks.
 *
 * Unlike Claude Code and Codex, opencode lets a plugin change the model of a
 * single message, so here the router really switches: it moves the message onto
 * the helper agent for Jev's size, pinned to that size's model, instead of asking
 * the main model to delegate.
 *
 * The sizing itself lives in jev_router.py, shared with the other harnesses; this
 * module only applies its answer. It swallows every failure, because opencode
 * drops a message whose chat.message hook throws, and the router must never do
 * that.
 */

import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const pluginRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const scripts = path.join(pluginRoot, 'skills', 'jev', 'scripts');
const ROUTER = ['uv', 'run', '--quiet', '--script', path.join(scripts, 'jev_router.py'), 'hook', 'opencode'];
// Reason: the script abandons Jev at 6 s; this caps the whole run, uv startup and
// logging included, at the same 8 s the other harnesses' hook configs allow.
const HARD_LIMIT_MS = 8_000;
// Reason: only the default coding agent's messages are routed. Moving a plan-mode
// message onto a helper would lift plan mode's read-only limits.
const ROUTABLE_AGENT = 'build';
// Reason: route only when a person is typing. The TUI runs its sessions in this
// worker script, while `opencode run` (reviews, automation) runs src/index.js with
// its own pinned model and variant (verified). Anything else fails closed.
const TUI_WORKER = /cli[\\/]tui[\\/]worker\.js$/;
const BASE62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';

function readTiers() {
  const data = JSON.parse(fs.readFileSync(path.join(scripts, 'tiers.json'), 'utf8'));
  return data.harnesses.opencode.map((tier) => ({ ...tier, jobs: data.jobs[tier.size] }));
}

/** Run the shared router on one message; resolve to its decision, or null for "carry on". */
function askRouter(prompt) {
  return new Promise((resolve) => {
    let out = '';
    let child;
    try {
      child = spawn(ROUTER[0], ROUTER.slice(1), { stdio: ['pipe', 'pipe', 'ignore'] });
    } catch {
      resolve(null);
      return;
    }
    const timer = setTimeout(() => {
      child.kill();
      resolve(null);
    }, HARD_LIMIT_MS);
    child.on('error', () => {
      clearTimeout(timer);
      resolve(null);
    });
    child.stdout.on('data', (chunk) => {
      out += chunk;
    });
    child.on('close', () => {
      clearTimeout(timer);
      try {
        resolve(out.trim() ? JSON.parse(out) : null);
      } catch {
        resolve(null);
      }
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify({ prompt }));
  });
}

/**
 * A part id that sorts right after the message's last part. opencode ids are
 * `prt_` + 12 hex digits of time + 14 random base62 characters.
 */
function nextPartId(parts) {
  const match = /^prt_([0-9a-f]{12})/.exec(parts.at(-1)?.id ?? '');
  if (!match) return null;
  const time = (BigInt(`0x${match[1]}`) + 1n).toString(16).padStart(12, '0');
  const tail = Array.from(crypto.randomBytes(14), (byte) => BASE62[byte % 62]).join('');
  return `prt_${time}${tail}`;
}

export default async function jevRouter() {
  let tiers;
  try {
    tiers = readTiers();
  } catch {
    // A broken install must not take the other plugins' skills down with it.
    return {};
  }

  return {
    config: async (config) => {
      config.agent = config.agent ?? {};
      for (const tier of tiers) {
        // Reason: chat.message can move a message onto a hidden subagent (verified),
        // so the helpers stay out of the Tab list of primary agents.
        config.agent[tier.helper] ??= {
          mode: 'subagent',
          hidden: true,
          model: tier.model_id,
          description: `Jev router helper for ${tier.size} jobs (${tier.jobs}). Runs on ${tier.model} at ${tier.effort} thinking.`,
        };
      }
      config.command = config.command ?? {};
      config.command.jev ??= {
        description: 'Turn the Jev router on or off, or show its status',
        template: 'Use the jev skill to run the Jev router command: $ARGUMENTS',
      };
    },

    'chat.message': async (_input, output) => {
      try {
        if (!TUI_WORKER.test(process.argv[1] ?? '')) return;
        if ((output.message.agent ?? ROUTABLE_AGENT) !== ROUTABLE_AGENT) return;
        const prompt = output.parts
          .filter((part) => part.type === 'text' && !part.synthetic)
          .map((part) => part.text)
          .join('\n');
        const decision = await askRouter(prompt);
        const id = decision && nextPartId(output.parts);
        if (!id) return;
        // A decision without a model keeps the message where it is, with a note.
        if (decision.model_id) {
          const slash = decision.model_id.indexOf('/');
          output.message.agent = decision.agent;
          // Reason: the thinking level lives on the message's model as `variant`
          // (verified: a top-level message.variant is ignored).
          output.message.model = {
            providerID: decision.model_id.slice(0, slash),
            modelID: decision.model_id.slice(slash + 1),
            ...(decision.variant ? { variant: decision.variant } : {}),
          };
        }
        output.parts.push({
          id,
          sessionID: output.message.sessionID,
          messageID: output.message.id,
          type: 'text',
          text: decision.context,
          synthetic: true,
        });
      } catch {
        // The router must never block a message: any failure means "carry on".
      }
    },
  };
}
