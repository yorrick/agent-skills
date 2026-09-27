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
// Reason: the script abandons Jev at 6 s; this caps the whole decision (uv
// startup, logging and opencode's own client calls included) at the same 8 s the
// other harnesses' hook configs allow.
const HARD_LIMIT_MS = 8_000;
// Reason: only the default coding agent's messages are routed. Moving a plan-mode
// message onto a helper would lift plan mode's read-only limits.
const ROUTABLE_AGENT = 'build';
// Reason: route only when a person is typing, and require two signals. The TUI
// runs its server in this worker script, while a plain `opencode run` (reviews,
// automation) runs src/index.js with its own pinned model and variant. But
// `opencode run --attach` sends its message to a server a TUI started, so the
// message must also name its agent: the TUI always does, `opencode run` only with
// an explicit --agent (both verified). Anything else fails closed.
const TUI_WORKER = /cli[\\/]tui[\\/]worker\.js$/;
const BASE62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';
const INHERITED_FROM_BUILD = ['permission', 'tools', 'prompt', 'steps', 'maxSteps'];

function pick(object, keys) {
  return Object.fromEntries(keys.filter((key) => key in object).map((key) => [key, object[key]]));
}

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

/**
 * Every "provider/model" this opencode can run right now. config.providers()
 * lists only providers with credentials (verified: OpenRouter disappears without
 * a login), answers in about 10 ms, and is safe to call from chat.message.
 */
async function usableModels(client) {
  const { data } = await client.config.providers();
  const usable = new Set();
  for (const provider of data?.providers ?? []) {
    for (const modelID of Object.keys(provider.models ?? {})) usable.add(`${provider.id}/${modelID}`);
  }
  return usable;
}

/**
 * Providers with credentials. config.providers() also lists a provider the user
 * merely declared in opencode.json, so a switch additionally needs the provider
 * in provider.list()'s `connected` (verified: OpenRouter drops out without a
 * login). That call takes about 300 ms, so it runs alongside the Jev call.
 */
async function connectedProviders(client) {
  const { data } = await client.provider.list();
  return new Set(data?.connected ?? []);
}

/**
 * What to do with one message, worked out without touching it: the router's
 * decision if opencode can apply it, or null.
 *
 * The router's own OpenRouter key is separate from opencode's logins, so Jev can
 * answer while opencode cannot run the tier's model, and switching then would
 * break a working session. With no usable tier the message is not even sent to
 * Jev.
 */
async function decide(client, tiers, prompt) {
  const usable = await usableModels(client);
  if (!tiers.some((tier) => usable.has(tier.model_id))) return null;
  const [decision, connected] = await Promise.all([
    askRouter(prompt),
    connectedProviders(client).catch(() => new Set()),
  ]);
  if (!decision?.model_id) return decision;
  const provider = decision.model_id.split('/')[0];
  return usable.has(decision.model_id) && connected.has(provider) ? decision : null;
}

/** The promise's value, or null once `ms` have passed or if it fails. */
function within(ms, promise) {
  let timer;
  const expired = new Promise((resolve) => {
    timer = setTimeout(() => resolve(null), ms);
  });
  return Promise.race([promise.catch(() => null), expired]).finally(() => clearTimeout(timer));
}

export default async function jevRouter({ client } = {}) {
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
        // Reason: a helper is the user's build agent on another model. It copies
        // build's restrictions and instructions so routing never lifts a limit the
        // user put on build, but none of its model-specific settings (options,
        // temperature, variant), which could clash with the tier's own model and
        // thinking level. chat.message can move a message onto a hidden subagent
        // (verified), so helpers stay out of the Tab list of primary agents.
        config.agent[tier.helper] ??= {
          ...pick(config.agent[ROUTABLE_AGENT] ?? {}, INHERITED_FROM_BUILD),
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

    'chat.message': async (input, output) => {
      try {
        if (!TUI_WORKER.test(process.argv[1] ?? '') || input?.agent === undefined) return;
        if ((output.message.agent ?? ROUTABLE_AGENT) !== ROUTABLE_AGENT) return;
        const prompt = output.parts
          .filter((part) => part.type === 'text' && !part.synthetic)
          .map((part) => part.text)
          .join('\n');
        // Reason: one deadline covers the whole decision, opencode's own client
        // calls included, and the message is only changed after it is known, so a
        // late answer can never switch a message that has already gone ahead.
        const decision = await within(HARD_LIMIT_MS, decide(client, tiers, prompt));
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
