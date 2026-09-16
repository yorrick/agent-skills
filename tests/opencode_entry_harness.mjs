// Fixture for tests/test_opencode_entry.py. Loads the opencode plugin entry,
// runs its config hook against a fresh config, again against the same config
// (idempotency), and once against a config that already defines a `workflow`
// command (user commands must win). Prints the result as JSON.
//
// Run with cwd set to the repository root.

import path from 'node:path';
import { pathToFileURL } from 'node:url';

const repoRoot = process.cwd();
const entry = path.join(repoRoot, '.opencode', 'plugins', 'agent-skills.js');
const module = await import(pathToFileURL(entry).href);
const hooks = await module.AgentSkillsPlugin({ client: {}, directory: repoRoot });

const fresh = {};
await hooks.config(fresh);
const afterFirstPass = JSON.parse(JSON.stringify(fresh));
await hooks.config(fresh);

const seeded = { command: { workflow: { template: 'USER TEMPLATE' } } };
await hooks.config(seeded);

process.stdout.write(
  JSON.stringify({
    exports: Object.fromEntries(
      Object.entries(module).map(([name, value]) => [name, typeof value]),
    ),
    config: fresh,
    configAfterSecondPass: afterFirstPass,
    seeded,
  }),
);
