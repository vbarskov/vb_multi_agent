import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdtemp, mkdir, readFile, rm, symlink } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const skillName = 'vb-multi-agent';
const skillDirectory = path.join(root, 'plugins', skillName, 'skills', skillName);

function frontmatter(text) {
  const block = text.match(/^---\r?\n([\s\S]*?)\r?\n---/)?.[1] ?? '';
  return {
    name: block.match(/^name:\s*(.+)$/m)?.[1]?.trim(),
    description: block.match(/^description:\s*(.+)$/m)?.[1]?.trim(),
  };
}

test('корневой пакет объявляет DSH-слой, точку входа и патч', async () => {
  const pkg = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));
  assert.equal(pkg.name, skillName);
  assert.equal(pkg.type, 'module');
  assert.equal(pkg.dsh.bundle.patch, './dsh/cordis.patch.yml');
  assert.equal(pkg.main, './dsh/lib/index.js');
  assert.deepEqual(pkg.exports, { '.': './dsh/lib/index.js', './package.json': './package.json' });
  assert.ok(existsSync(path.join(root, 'dsh', 'cordis.patch.yml')));

  const patch = await readFile(path.join(root, 'dsh/cordis.patch.yml'), 'utf8');
  assert.match(patch, /- insert:/);
  assert.match(patch, new RegExp(`^ {4}- id: ${skillName}$`, 'm'));
  assert.match(patch, new RegExp(`^ {6}name: ${skillName}$`, 'm'));
});

test('DSH-слой объявлен только в корне репозитория', () => {
  assert.equal(existsSync(path.join(root, 'plugins', skillName, 'package.json')), false);
  assert.equal(existsSync(path.join(root, 'plugins', skillName, 'dsh')), false);
});

test('манифесты Codex, Kimi и DSH называют одну версию', async () => {
  const read = relative => readFile(path.join(root, relative), 'utf8').then(JSON.parse);
  const pkg = await read('package.json');
  for (const manifest of ['kimi.plugin.json', 'plugins/vb-multi-agent/kimi.plugin.json', 'plugins/vb-multi-agent/plugin.json']) {
    const value = await read(manifest);
    assert.equal(value.version, pkg.version, `${manifest}: версия не совпадает с package.json`);
    assert.equal(value.name, pkg.name, `${manifest}: имя не совпадает с package.json`);
  }
});

test('пакет импортируется по имени так же, как его загружает DSH', async () => {
  const home = await mkdtemp(path.join(tmpdir(), 'dsh-import-'));
  try {
    await mkdir(path.join(home, 'node_modules'), { recursive: true });
    await symlink(root, path.join(home, 'node_modules', skillName), 'dir');
    const result = spawnSync(
      process.execPath,
      ['--input-type=module', '-e', `const m = await import('${skillName}'); console.log(typeof m.apply, m.name, m.inject.join(','))`],
      { cwd: home, encoding: 'utf8' },
    );
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout.trim(), `function ${skillName} skills`);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test('плагин регистрирует навык с метаданными из SKILL.md', async () => {
  const plugin = await import('../dsh/lib/index.js');
  assert.equal(plugin.name, skillName);
  assert.deepEqual(plugin.inject, ['skills']);

  const providers = [];
  plugin.apply({ skills: { registerProvider(factory) { providers.push(factory()); } } });
  assert.deepEqual(providers.map(provider => provider.name), [skillName]);

  const [candidate] = await providers[0].list();
  assert.equal(candidate.name, skillName);
  assert.equal(candidate.provider, skillName);
  assert.equal(candidate.source, 'bundled');
  assert.equal(candidate.rank, 600);
  assert.equal(candidate.invocation.modelInvocable, true);
  assert.equal(candidate.invocation.userInvocable, true);
  assert.equal(candidate.resourceBase.path, path.join(skillDirectory, '/'));

  const text = await readFile(candidate.path, 'utf8');
  assert.deepEqual(frontmatter(text), { name: candidate.name, description: candidate.description });
  assert.ok(candidate.description.length > 0);

  const loaded = await providers[0].get();
  assert.equal(loaded.name, candidate.name);
  assert.equal(loaded.description, candidate.description);
  assert.ok(loaded.content.trim().length > 0);
  assert.equal(loaded.content.trimStart().startsWith('---'), false);
});

test('навык указывает на существующие скрипты запуска', () => {
  assert.ok(existsSync(path.join(skillDirectory, 'scripts', 'run.py')));
  assert.ok(existsSync(path.join(skillDirectory, 'scripts', 'config.example.json')));
});
