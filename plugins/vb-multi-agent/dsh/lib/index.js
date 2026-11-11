/**
 * DSH-слой VB Multi Agent.
 *
 * Регистрирует навык `vb-multi-agent` как bundled-провайдер `ctx.skills`,
 * чтобы он был доступен в DeepSeek Harness в любом рабочем каталоге.
 * Инструкции читаются из того же `skills/vb-multi-agent/SKILL.md`, что
 * используют Codex и Kimi Code, поэтому текст один.
 *
 * Контракт реестра DSH: у провайдера есть `name`, и каждый кандидат из
 * `list()` обязан вернуть это же значение в поле `provider`. Иначе реестр
 * отклоняет каталог навыков на каждом ходу — и падает каждое сообщение.
 *
 * Навык ссылается на `scripts/run.py` из своего каталога, поэтому в
 * `resourceBase` передаётся абсолютный путь каталога навыка.
 *
 * Пакет импортирует только встроенные модули Node.js и собственные файлы
 * репозитория: так `link:`-установка работает без зависимостей в профиле DSH.
 */
import { readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

/** Навык плагина; каталог `skills/<имя>` и `name` в SKILL.md должны совпадать. */
export const SKILL_NAME = 'vb-multi-agent';

/** Ранг bundled-провайдера из `@deepseek-ai/dsh-skill`. */
const BUNDLED_SKILL_RANK = 600;

/** Доступ из модели и из пользовательского `/`. */
const INVOCATION = { modelInvocable: true, userInvocable: true };

/** YAML-описание навыка в начале `SKILL.md`. */
const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

/** Публичная грамматика имён навыков. */
const SKILL_NAME_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/** URL файла навыка. */
function skillFile() {
  return new URL(`../../skills/${SKILL_NAME}/SKILL.md`, import.meta.url);
}

/** Абсолютный путь каталога навыка: относительно него работают `scripts/…`. */
function skillDirectory() {
  return fileURLToPath(new URL(`../../skills/${SKILL_NAME}/`, import.meta.url));
}

/** Убрать YAML-описание, оставив тело инструкций. */
function stripFrontmatter(text) {
  return text.replace(FRONTMATTER, '');
}

/** Разобрать обязательные поля YAML-описания. */
function parseFrontmatter(text) {
  const block = text.match(FRONTMATTER)?.[1];
  if (!block) throw new Error(`vb-multi-agent: у навыка ${SKILL_NAME} нет YAML-описания`);
  const name = block.match(/^name:\s*(.+)$/m)?.[1]?.trim();
  const description = block.match(/^description:\s*(.+)$/m)?.[1]?.trim();
  if (name !== SKILL_NAME) throw new Error(`vb-multi-agent: имя навыка ${name ?? '(нет)'} не совпадает с каталогом ${SKILL_NAME}`);
  if (!description) throw new Error(`vb-multi-agent: у навыка ${SKILL_NAME} нет описания`);
  return { description };
}

/** Прочитать файл навыка синхронно: так ошибка видна при монтировании. */
function readSkillFile() {
  try {
    return readFileSync(skillFile(), 'utf8');
  } catch (error) {
    throw new Error(`vb-multi-agent: не удалось прочитать навык ${SKILL_NAME}: ${error.message}`, { cause: error });
  }
}

/** Проверить навык до регистрации провайдера. */
function validateSkill() {
  if (!SKILL_NAME_PATTERN.test(SKILL_NAME)) throw new Error(`vb-multi-agent: недопустимое имя навыка ${SKILL_NAME}`);
  const text = readSkillFile();
  parseFrontmatter(text);
  if (!stripFrontmatter(text).trim()) throw new Error(`vb-multi-agent: навык ${SKILL_NAME} не содержит инструкций`);
}

/** Загрузить навык целиком: описание, путь и тело инструкций. */
async function loadSkill() {
  const file = skillFile();
  const text = await readFile(file, 'utf8');
  const { description } = parseFrontmatter(text);
  const body = stripFrontmatter(text);
  if (!body.trim()) throw new Error(`vb-multi-agent: навык ${SKILL_NAME} не содержит инструкций`);
  return {
    name: SKILL_NAME,
    description,
    invocation: INVOCATION,
    // Реестр DSH требует, чтобы кандидат указывал имя своего провайдера.
    provider: SKILL_NAME,
    source: 'bundled',
    resourceBase: { kind: 'directory', path: skillDirectory() },
    path: fileURLToPath(file),
    content: body,
  };
}

/** Провайдер навыка: реестр DSH вызывает `list` и `get`. */
const provider = {
  name: SKILL_NAME,
  async list() {
    const { content: _content, ...summary } = await loadSkill();
    return [{ ...summary, rank: BUNDLED_SKILL_RANK, locator: skillFile() }];
  },
  async get() {
    return loadSkill();
  },
};

/** Имя Cordis-плагина в списке плагинов профиля. */
export const name = 'vb-multi-agent';

/** Реестр навыков, который заполняет этот плагин. */
export const inject = ['skills'];

/**
 * Проверить инструкции и зарегистрировать провайдера навыка.
 * @param {import('@deepseek-ai/cordis').Context} ctx - контекст с `ctx.skills`.
 */
export function apply(ctx) {
  validateSkill();
  ctx.skills.registerProvider(() => provider);
}
