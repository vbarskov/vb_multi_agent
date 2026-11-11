# VB Multi Agent

Локальный плагин Codex, Kimi Code и DeepSeek Harness для параллельной разработки в Git. Router разбивает задачу на независимые части. Разработчики параллельно редактируют назначенные файлы в своих копиях. Runner объединяет проверенные изменения, tester проверяет результат, а reviewer читает код и общий diff и указывает замечания с файлом и строкой. У каждой роли свой CLI и модель. Параллельными процессами управляет Python, поэтому навык можно вызвать и на Low.

## Установка

### В Kimi Code

Введите в командной строке Kimi Code:

```text
/plugins install https://github.com/vbarskov/vb_multi_agent
```

Также можно установить локальную копию, указав путь к корню репозитория:

```text
/plugins install /Users/victorbarskov/Developer/vb_multi_agent
```

После установки выполните `/reload` или начните новый чат. Вызовите навык через `/skill:vb-multi-agent:vb-multi-agent` и передайте задачу. Если настроили роли, укажите абсолютный путь к JSON-конфигу.

При изменении исходников установите плагин повторно: Kimi Code использует управляемую копию, а не исходный каталог.

### В Codex

Из терминала зарегистрируйте локальный каталог.

```sh
codex plugin marketplace add /Users/victorbarskov/Developer/vb_multi_agent
```

В каталоге плагинов Codex выберите источник **VB Multi Agent**, установите **vb-multi-agent** и начните новый чат в целевом репозитории. Через `$` выберите навык `vb-multi-agent`. Передайте задачу. Если настроили роли, укажите абсолютный путь к JSON-конфигу.

```text
$vb-multi-agent Добавь проверку срока действия токена. Конфиг ролей в /absolute/path/to/roles.json.
```

### В DeepSeek Harness

Пакет DeepSeek Harness — корень репозитория: `package.json` объявляет `dsh.bundle.patch`, поэтому профиль получает навык из `plugins/vb-multi-agent/skills/vb-multi-agent/SKILL.md` как bundled-провайдер. Отдельно править `cordis.patch.yml` не нужно.

Настольное приложение использует профиль `desktop`, запуск `dsh web` из терминала — `web`. Ниже — `desktop`. Отдельно ставить пакетный менеджер не нужно: `dsh plugin` использует встроенный pnpm.

Установка из GitHub:

```sh
DSH="/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
"$DSH" plugin --profile desktop add github:vbarskov/vb_multi_agent
```

Для работы с локальными правками поставьте каталог ссылкой — тогда изменения видны без переустановки (`link:` не копирует файлы). Путь указывайте абсолютный: относительный pnpm разрешает от каталога профиля.

```sh
"$DSH" plugin --profile desktop add link:/полный/путь/vb_multi_agent
```

`plugin add` сам дописывает `vb-multi-agent` в `dependencies` и `dsh.profile.bundles` файла `~/.dsh/profiles/desktop/package.json`; проверьте обе записи. Затем начните новый чат: навык появится в каталоге и будет доступен как `/vb-multi-agent` или через инструмент `skill`. Если навык не появился, перезапустите DeepSeek Harness: новый бандл подхватывается горячей перезагрузкой не во всех случаях.

```text
/vb-multi-agent Добавь проверку срока действия токена. Конфиг ролей в /absolute/path/to/roles.json.
```

Тот же установщик есть в интерфейсе: **боковая панель → Плагины → Добавить плагин → Установите сторонний плагин**, в поле «Название или адрес пакета» — `https://github.com/vbarskov/vb_multi_agent` или локальный путь к корню репозитория, затем **Установить** и **Включить сейчас**.

Обновление: для `link:` достаточно `git pull`; после новых коммитов в GitHub удалите `github:`-установку и поставьте заново. Удаление: `"$DSH" plugin --profile desktop remove vb-multi-agent`, запись из `bundles` уходит сама.

## Настройка ролей

Встроенный конфиг запускает все роли через Codex CLI. Чтобы выбрать модели и провайдеров, скопируйте [пример конфига](plugins/vb-multi-agent/skills/vb-multi-agent/scripts/config.example.json) в файл вне целевого репозитория. В `roles` укажите отдельные настройки для `router`, `developer`, `tester` и `reviewer`. Разные роли могут использовать разные CLI.

| Поле роли | Что задаёт |
| --- | --- |
| `backend` | `codex`, `kimi`, `dsh` или `custom`. |
| `model` | Идентификатор модели, которую поддерживает выбранный CLI. Если поле опущено, используется его модель по умолчанию. |
| `reasoning` | Поддерживаемый выбранным CLI режим рассуждения. Для `dsh` нужен явный `model`. |
| `executable` | Явный путь или имя исполняемого файла для `codex`, `kimi` или `dsh`. Имеет приоритет над автоматическим поиском. |
| `provider` | Провайдер модели в `dsh`. Требуется явный `model`. |
| `command` | Для `custom` массив аргументов команды с необязательными подстановками `{model}` и `{reasoning}`. Команда читает запрос из stdin и печатает ответ в stdout. |

`max_parallel` ограничивает число одновременно работающих разработчиков. `timeout_seconds` задаёт тайм-аут отдельного вызова агента.

Необязательное поле верхнего уровня `git_executable` задаёт абсолютный путь к исполняемому файлу Git. При автоматическом выборе на macOS runner предпочитает самостоятельный Git из `PATH`, затем пробует `/Library/Developer/CommandLineTools/usr/bin/git`. Системный `/usr/bin/git` пропускается, чтобы избежать запуска поиска инструментов Xcode. Для каждого запуска runner использует выбранный Git и подставляет его для команд Git в оболочке агентов.

Пример смешанного конфига. Замените алиасы моделей на доступные в ваших CLI.

```json
{
  "max_parallel": 3,
  "timeout_seconds": 600,
  "roles": {
    "router": {"backend": "codex", "model": "gpt-6.1-sol", "reasoning": "low"},
    "developer": {"backend": "kimi", "model": "your-kimi-alias"},
    "tester": {"backend": "dsh", "model": "your-deepseek-model"},
    "reviewer": {"backend": "codex", "model": "gpt-6.1-sol", "reasoning": "high"}
  }
}
```

Нужны Git, Python 3.10+ и установленные, авторизованные CLI выбранных ролей. Используйте выбранные провайдеры только для репозиториев, код которых им можно передавать. Для `custom` выбирайте доверенную команду, которая соблюдает режим доступа из `VB_MULTI_AGENT_ACCESS`. Developer получает `workspace-write` и пишет только в выделенную копию. Остальные роли получают `read-only`.

- `codex` использует сохранённую авторизацию Codex CLI и запускается без пользовательского `config.toml`. Предварительная проверка вызывает `codex --version`. Если рабочий Codex отсутствует в `PATH`, на macOS runner пробует CLI, встроенный в приложение ChatGPT. Чтобы выбрать конкретный CLI, задайте `executable`. Уровень `reasoning` должен поддерживаться выбранной моделью. Для собственного маршрута из `config.toml` задайте доверенную команду через `custom`.
- `kimi` использует актуальный Kimi Code CLI от Moonshot. В `model` передайте алиас модели. Для маршрутов, поддерживающих настройку рассуждения Kimi, доступны `low`, `medium`, `high`, `xhigh` и `max`. Для остальных опустите `reasoning`.
- `dsh` требует DeepSeek Harness с поддержкой `--patch` и режимов `read-only` и `workspace-write` при headless-запуске. Для официального провайдера `deepseek-official` настройте `DEEPSEEK_API_KEY` в окружении или `.env` DeepSeek Harness.

## Запуск из терминала

Тот же запуск доступен из окружения Kimi или DeepSeek Harness. Проверьте конфиг до обращения к моделям. Для настроек по умолчанию опустите `--config`.

```sh
vb_agent_runner=/Users/victorbarskov/Developer/vb_multi_agent/plugins/vb-multi-agent/skills/vb-multi-agent/scripts/run.py
python3 "$vb_agent_runner" --check --config /absolute/path/to/roles.json
python3 "$vb_agent_runner" --repo /absolute/path/to/repo --config /absolute/path/to/roles.json \
  --task 'Добавь проверку срока действия токена и обработай истёкший токен в клиенте'
```

Целевой репозиторий должен иметь коммит и чистое рабочее дерево. Временные копии содержат файлы исходного коммита. Подмодули остаются без содержимого. Runner собирает Git diff из каждой копии разработчика, проверяет пути изменений и объединяет их в порядке плана. Tester и reviewer получают объединённый результат в режиме чтения.

Если пользователь разрешил конкретную команду проверки, передайте её аргументы как JSON-массив, например `--test-command '["python3","-m","unittest"]'`. Ключ можно повторить. Без него tester анализирует изменения и предлагает проверки.

По завершении runner печатает путь к каталогу с `changes.patch` и `report.json`. Для своего каталога используйте `--out /absolute/path/to/new-directory`. Просмотрите отчёт и патч перед применением. Исходный репозиторий обновляется отдельной командой `git apply`, которую можно предварить `git apply --check` на той же базе.
