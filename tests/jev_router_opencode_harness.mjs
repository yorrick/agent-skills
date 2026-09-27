// Fixture for tests/test_jev_router.py. Loads the opencode plugin entry, runs its
// config hook, then runs chat.message on one user message the way opencode does,
// and prints the resulting config and message as JSON.
//
// Usage (cwd = repository root):
//   node jev_router_opencode_harness.mjs <agent> <prompt> <tui|run|attached>
// The last argument stands in for how the message reached opencode:
//   tui       the TUI: its server runs in src/cli/tui/worker.js and it names the agent
//   run       `opencode run`: its own server in src/index.js, no agent named
//   attached  `opencode run --attach` to a TUI's server: the worker, no agent named

import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [agent, prompt, mode] = process.argv.slice(2);
process.argv[1] = mode === 'run' ? '/opt/opencode/src/index.js' : '/opt/opencode/src/cli/tui/worker.js';
const repoRoot = process.cwd();
const entry = path.join(repoRoot, '.opencode', 'plugins', 'agent-skills.js');
const module = await import(pathToFileURL(entry).href);
// opencode's client, reduced to the two calls the router makes: the configured
// providers with their models (JEV_TEST_PROVIDERS) and the ids of providers that
// have credentials (JEV_TEST_CONNECTED). Defaults match the user's machine:
// OpenRouter, logged in, with GLM 5.3 flash.
const providers = JSON.parse(
  process.env.JEV_TEST_PROVIDERS ?? '[{"id":"openrouter","models":{"z-ai/glm-5.3-flash":{}}}]',
);
const connected = JSON.parse(process.env.JEV_TEST_CONNECTED ?? '["openrouter"]');
const client = {
  config: { providers: async () => ({ data: { providers } }) },
  provider: { list: async () => ({ data: { connected } }) },
};
const hooks = await module.AgentSkillsPlugin({ client, directory: repoRoot });

// The user's own opencode config, as the config hook receives it (JSON, optional).
const config = JSON.parse(process.env.JEV_TEST_OPENCODE_CONFIG ?? '{}');
await hooks.config(config);

const ids = { message: 'msg_0e2fd833d001IPZdi1mHT14U08', session: 'ses_f1d027cf5ffefUQtzA1W61Ange' };
const output = {
  message: {
    id: ids.message,
    sessionID: ids.session,
    role: 'user',
    agent,
    model: { providerID: 'openrouter', modelID: 'deepseek/deepseek-v4.1-flash' },
  },
  parts: [
    { id: 'prt_0e2fd8341001ZqBA4dLFgwQqAM', messageID: ids.message, sessionID: ids.session, type: 'text', text: prompt },
  ],
};
// Verified: the TUI's chat.message input names the agent; `opencode run` omits it.
const input = mode === 'tui' ? { sessionID: ids.session, agent } : { sessionID: ids.session };
await hooks['chat.message'](input, output);

process.stdout.write(JSON.stringify({ agent: config.agent, command: config.command, output }));
