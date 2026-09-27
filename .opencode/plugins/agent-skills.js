/**
 * opencode plugin for the yorrick/agent-skills repository.
 *
 * opencode installs this repository as a git package and imports this file.
 * The config hook registers every plugin directory's skills and commands so
 * the repository works in opencode the same way it does in Claude Code and
 * Codex: no symlinks, no per-skill configuration.
 *
 * A top-level directory is a plugin if and only if it contains plugin.toml,
 * the same invariant scripts/sync_manifests.py enforces. That excludes docs/,
 * scripts/, tests/, and any local-only directory automatically.
 *
 * The same file also works as a project plugin when opencode is run from a
 * checkout of this repository, because the package root is derived from this
 * file's own location.
 *
 * A plugin that needs opencode hooks ships them as code in hooks/opencode.js,
 * next to hooks/hooks.json (Claude Code) and hooks/codex.json (Codex). Its
 * default export takes the plugin input and returns hooks; this entry merges
 * them, running each hook in plugin order after its own config work.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(dirname, '..', '..');
const PLUGIN_MARKER = 'plugin.toml';
const PLUGIN_ROOT = '${CLAUDE_PLUGIN_ROOT}';
const HOOK_MODULE = path.join('hooks', 'opencode.js');

function pluginDirectories() {
  return fs
    .readdirSync(repoRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(repoRoot, entry.name))
    .filter((dir) => fs.existsSync(path.join(dir, PLUGIN_MARKER)));
}

function parseCommandFile(source) {
  const match = source.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
  if (!match) return { body: source, description: undefined };
  let description;
  for (const line of match[1].split(/\r?\n/)) {
    const colon = line.indexOf(':');
    if (colon <= 0 || line.slice(0, colon).trim() !== 'description') continue;
    const value = line.slice(colon + 1).trim();
    // Only single-line scalars are mapped; a YAML block scalar (`|` or `>`)
    // would otherwise land a literal "|" in the command menu.
    if (!value || value.startsWith('|') || value.startsWith('>')) continue;
    description = value.replace(/^["']|["']$/g, '');
  }
  return { body: source.slice(match[0].length), description };
}

async function loadPluginHooks(directories, input) {
  const hookSets = [];
  for (const dir of directories) {
    const file = path.join(dir, HOOK_MODULE);
    if (!fs.existsSync(file)) continue;
    const module = await import(pathToFileURL(file).href);
    hookSets.push(await module.default(input));
  }
  return hookSets;
}

/** One hook per name: each plugin's version runs in turn on the same arguments. */
function mergeHooks(hookSets) {
  const merged = {};
  for (const name of new Set(hookSets.flatMap((hooks) => Object.keys(hooks)))) {
    const handlers = hookSets.map((hooks) => hooks[name]).filter(Boolean);
    merged[name] = async (...args) => {
      for (const handler of handlers) await handler(...args);
    };
  }
  return merged;
}

export const AgentSkillsPlugin = async (input) => {
  let directories;

  const allDirectories = () => (directories ??= pluginDirectories());

  const registerSkills = (config) => {
    config.skills = config.skills ?? {};
    const paths = (config.skills.paths = config.skills.paths ?? []);
    for (const dir of allDirectories()) {
      const skills = path.join(dir, 'skills');
      if (fs.existsSync(skills) && !paths.includes(skills)) paths.push(skills);
    }
  };

  const registerCommands = (config) => {
    config.command = config.command ?? {};
    for (const dir of allDirectories()) {
      const commands = path.join(dir, 'commands');
      if (!fs.existsSync(commands)) continue;
      for (const entry of fs.readdirSync(commands, { withFileTypes: true })) {
        if (!entry.isFile() || !entry.name.endsWith('.md')) continue;
        const name = entry.name.slice(0, -'.md'.length);
        if (Object.hasOwn(config.command, name)) continue;
        const source = fs.readFileSync(path.join(commands, entry.name), 'utf8');
        const { body, description } = parseCommandFile(source);
        const command = { template: body.split(PLUGIN_ROOT).join(dir) };
        if (description !== undefined) command.description = description;
        config.command[name] = command;
      }
    }
  };

  const { config: pluginConfig, ...pluginHooks } = mergeHooks(await loadPluginHooks(allDirectories(), input));

  return {
    ...pluginHooks,
    config: async (config) => {
      registerSkills(config);
      registerCommands(config);
      await pluginConfig?.(config);
    },
  };
};
