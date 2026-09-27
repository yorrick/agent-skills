// Fixture for tests/test_jev_router.py. Loads the opencode plugin entry, runs its
// config hook, then runs chat.message on one user message the way opencode does,
// and prints the resulting config and message as JSON.
//
// Usage (cwd = repository root):
//   node jev_router_opencode_harness.mjs <agent> <prompt> <tui|run>
// The last argument stands in for how opencode was started: the TUI runs sessions
// in src/cli/tui/worker.js, while `opencode run` runs src/index.js.

import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [agent, prompt, mode] = process.argv.slice(2);
process.argv[1] = mode === 'tui' ? '/opt/opencode/src/cli/tui/worker.js' : '/opt/opencode/src/index.js';
const repoRoot = process.cwd();
const entry = path.join(repoRoot, '.opencode', 'plugins', 'agent-skills.js');
const module = await import(pathToFileURL(entry).href);
const hooks = await module.AgentSkillsPlugin({ client: {}, directory: repoRoot });

const config = {};
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
await hooks['chat.message']({ sessionID: ids.session }, output);

process.stdout.write(JSON.stringify({ agent: config.agent, command: config.command, output }));
