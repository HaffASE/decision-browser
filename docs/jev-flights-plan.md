# План рефакторинга: Jev-подобный цикл и подтверждённый Google Flights

Версия1, 10 октября2026. База9f6551d93af1ac0c4b86a35f44a097b1d29d8a10. Reference Jev1231850a0bf1a0c0341fe408ef1668dbbfdfac46. Статус: draft → независимое architecture review → независимое critic review. Реализация пользователем запрошена отдельно; публикация/merge не входят в этот запуск.

## Результат и границы

Один настоящий поиск Google Flights, начатый с пустой страницы поиска: JFK→LAX,10 ноября2026,1 взрослый,эконом,в одну сторону. Управляющий цикл сам наблюдает страницу, выбирает операцию/цель через OpenAI Decisions, вводит заданные данные и подтверждает, что показаны подходящие результаты. Подтверждение — зафиксированная форма маршрута/даты/пассажиров/класса и минимум одна видимая строка рейса с перевозчиком, временем и ценой/валютой, а не слово DONE или количество тестов. Ссылка/время проверки/commit и read-only доказательства сохраняются. Цена справочная, не гарантированная оферта.

Не включены: бронирование, покупка, login, изменение подписок, обход CAPTCHA, универсальная поддержка всех сайтов/iframe/shadowDOM, визуальная модель. В обычном планировании применяются принципы установленного ralplan; доступная среда не предоставляет native agent_type и проверяемую OMX role-routing authority. Последовательные независимые reviews — обычные рецензии, не поддельный host-consensus receipt.

## Principles / drivers / alternatives

Принципы: (1) внешний результат первым; (2) наблюдаемые DOM-цели и нативный ввод вместо придуманных селекторов/JS-действий; (3) Decisions выбирает действия, код связывает их с конкретными элементами; (4) защита относится к выбранному действию, декоративный неподдержанный узел не делает весь сайт неисполнимым; (5) доказательства и ограничения честно отделены.

Главные drivers: завершить динамический Flights сценарий; не сломать single-dispatch/guard/privacy/admission; уменьшить лишние abstractions и prompt duplication.

A. Перенести весь Jev browser harness/agent и заменить provider. Быстрое сходство, но две реализации admission/журнала/приватности, новая зависимость, риск ослабить текущие границы. Отклонено.
B. Исправить существующий DOM/action loop по проверенным принципам Jev, выделить compact action context, оставить production RunManager/DOMSession/OpenAI codec. Выбрано: минимальная архитектурная миграция с реальным результатом.
C. Написать Flights-specific Playwright сценарий или построить готовый search URL. Быстро доказывает сайт, но не агент/Decisions/ввод. Допустимо только как read-only исследование/fixture oracle, не acceptance.

Тension: строгий общий fail-closed против полезного ограниченного исполнения. Синтез: неизвестный выбранный target, popup, unsafe origin, изменённая семантика, неизвестный outcome всегда stop; невыбранные неподдержанные механизмы — диагностические исключения, никогда доступные actions.

## Наблюдаемые проблемы baseline

1. dom.py _COLLECT берёт первые81 совпадение широкого CSS до visibility, оставляет80. Сквозные role/скрытые элементы отсекают реальные calendar/options.
2. _loop останавливает страницу при любом unsupported: даже посторонний iframe/canvas/download.
3. _choose не получает историю исполненных действий; текущие values зависят от operation, helper видит только goal/name. Это провоцирует повторный ввод и ожидание вместо выбора autocomplete.
4. _dispatch_one не допускает same-origin JS/SPA Search navigation без exact href/form destination.
5. Нативный fill фокусирует поле, но не открывает его как click; не ждёт ограниченно появления подсказок. В Jev click→selectAll→insertText.
6. Имеющиеся функциональные checks не являются полноценным Flights oracle.

## Этапы реализации

### 1. Характеризация + acceptance contract
- Зафиксировать baseline, reference commit, основной source hash и начальные checks.
- Сохранить read-only наблюдение реальной Flights формы. Подтвердить роли полей, подсказок, даты, кнопки Search и результатного списка. Не считать CUA ручной поиск результатом программы.
- Добавить публичный synthetic request и отдельный read-only Flights evidence collector/validator. Поля задаются явно, model action generation запрещено заменять hardcoded route script.
- Negative oracle: не тот аэропорт/дата/trip-type, просто заполненная форма, loading, CAPTCHA, no flights, строка без цены — не PASS.

### 2. Практичный collector и target state
- Фильтровать видимые/активные/in-viewport интерактивные nodes до лимита; расширить ровно необходимые роли (combobox,option,gridcell,summary/contenteditable если подтверждено) и избегать контейнера gridcell если внутри button.
- Использовать bounded viewport text, текущие field values/selected/expanded и bounded ближайший dialog/list/form context. Не читать пароль/file/скрытые поля в provider context.
- Сохранить identity isolated CDP world/element handles и guard semantic comparison. Не переносить mutable window.__jevFast в hostile page main world.
- Разделить diagnostics (excluded unsupported node/capacity) и blocking page hazards (popup/context/unknown). Excluded nodes имеют пустые operations; скрытые узлы не забивают capacity. Количество пропусков явно в контексте и evidence, не выдавать неполное наблюдение за полное.

### 3. Jev-подобный decision context
- Выделить малый action-context модуль: один element record, operation-specific finite target mapping; не переписывать transport/codec без необходимости.
- В Decisions input: goal, URL/title/viewporttext, current field state, доступные операции и последние10 подтверждённо исполненных действий с наблюдаемым page_changed. Историю добавлять после acknowledge до post-observe; ambiguous attempt не выдумывать успешным.
- Правила выбора: не повторять удовлетворённые шаги; typed airport не равен выбранному airport; click calendar/date/confirm; Search после готовых полей; wait только пока нужное состояние отсутствует; DONE только предложение независимой проверки.
- Для live fixture Decisions-only: точные request.values по наблюдаемым labels; отсутствующее fill value останавливает run стабильной причиной. Никакого Responses fallback. Generic existing text helper отдельно; его контекст улучшить без включения в paid acceptance.
- Независимые finite target heads сохраняют официальный Decisions codec. Не копировать TypeSafe schema/criteria. Проверять stale после любого model call.

### 4. Нативное исполнение и динамические переходы
- Для text/combobox fill: свежий hit-test, реальный click/focus, native select-all/input; contenteditable только если корректно поддержан guard+selection. Пароли/file исключены.
- После input bounded settle для autocomplete/рендера с коротким deadline; не общий networkidle и не бесконечный sleep. Recording acknowledge раньше settle, поэтому не повторить действие при readback сбое.
- Разрешить ожидаемые same-origin SPA URL changes в явно task-scoped isolated run для уже разрешённого ordinary action. Без origin expansion/redirect grants, без ослабления standard-form exact destination contracts. Cross-origin/новое окно/неизвестный dispatch исход остаются stop.
- Добавить bounded no-progress detection по semantic observation+действию, не по UUID, с понятным evidence. Не прекращать легитимную загрузку сразу после одного wait.

### 5. Проверка и реальный прогон
- Native owned fixture воспроизводит hidden-prefix, autocomplete selection, calendar grid, same-origin SPA results и adversarial controls. Scripted provider тестирует механику; НЕ заменяет реальный Decisions.
- На итоговом дереве: targeted regressions→ruff/mypy→unit→full verifier, свежие evidence. Если cloud sandbox недоступен, Mac task запускает те же проверки; не отключатьsandbox.
- Live на пользовательском Mac с уже настроенным безопасным key injection (parent организует task). Ключ не выводить/копировать в облако/передавать в чат.
- Пользователь разрешил 40 Decisions calls,600sec,$0.25,безimages. Дополнительные bounds:40actions,512KiB aggregate request bytes. Цена официально $0.10/1M input; byte accounting не серверный dollar cap. Без неучтённых retries. При исчерпании stop; новый расход требует решения.
- Сохранить точный commit/sourcehash, configured limits, executed action trace безсекретов, Decisions usage/calls, итоговые structured checks, timestamps и results URL. Screenshot только локальный доказательный артефакт при разрешённой capture; модель images не получает. Provider DONE не PASS.
- Нативный повторный review изменённого кода после checks. Подготовить local commit; не push/merge без указания parent.

## Premortem / проверяемые риски

1. Collector видит первые элементы, но не календарь → hidden-prefix fixture и реальный observe на открытом calendar, capacity/omissions сообщаются; нельзя компенсировать промптом.
2. Модель бесконечно заполняет origin → history/currentvalue+selectedoption+pagechange; guard/no-progress; не выдавать typedquery за selection.
3. Safety фиксы разрешили нежелательное действие → negative tests cross-origin/purchase/booking/password/file/popup/stale/same-name node replacement, single-dispatch invariant; no new permanent site allowlist.

## Gate / definition of done

G1 independent architecture review complete; G2 independent critic APPROVE послеG1; G3 code+nativefixture+fullchecks; G4 реальный Mac Decisions Flights result independentlyverified. G4 BLOCKED/NOT_RUN означает задача не достигла основного результата; можно передать частично готовый код с точным blocker, без productionready/generalreliability claims.

Зависимости/unknown: Mac credential injection и session доступ проверяются отдельно. CAPTCHA/login не обходить. Если response schema/entitlement не соответствует официальному codec — stop с точным безопаснымerror и diagnostic, не fallback к другой модели.

## Версия2: обязательные уточнения после architecture review

R1. Compound fill — одна consumed попытка. Click начинает dispatch; перед selectAll/insertText повторно проверяются тот же Document, тот же backend DOM node, тип/editability, фокус именно на этом target и отсутствие popup/navigation. Не переводить фокус на replacement и не вводить в произвольный activeElement. Если click меняет семантику/фокус/target — текст не вводится, исход partial/unknown; pre-dispatch stale retry запрещён. Проще и безопаснее для collapsible combobox: отдельный Decisions click открывает picker, следующий observe даёт реальный editable target; fill уже открытого target сохраняет native focus/select/insert без повторного click, если click способен заменить node. Иной compound path допускается только с перечисленными guards. Тесты: focus-hijack/replacement/popup/navigation/failure-between-events; ни утечки текста, ни replay.

R2. SPA exception только task_scope+isolated+ordinary acknowledged action, canonical origin прежний И document identity прежний. Same-origin full navigation не исключение. Explicit href/form/navigate сохраняют exact destination match. Bounded post-action settling связывает delayedpushState с завершённым действием; вне этого окна переход при wait не автоматически разрешается. Нового navigation grant нет. Тесты immediate/delayedpushState/reload/crossorigin/popup/wrongexplicitdestination. Допускается сначала сохранить честный bounded limitation, если сайт требует full navigation, а не тихо расширить scope.

R3. Flights oracle отдельно от generic selection. Требует acknowledged Search в live trace, затем settled results; выбранные airport identitiesJFK/LAX (не typedquery/городвместоаэропорта), дата сгодом,triptype/adult/class и result-associated query summary совпадают. Текст form, изменённый поверх старых результатов, недостаточен. Carrier/time/price/currency извлекаются из одной visible row. Bounded raw evidence сохраняется рядом с нормализованнымиполями; ambiguous→NOT_VERIFIED. URL corroboration only. Negative stalepreviousquery/mixedrows/nearbyairports/uncommittedinput обязательны.

R4. Liveexperiment budget cumulative across attempts; single ledger outside process, failclosed if missing/corrupt on resume, noautomatic counterreset. Максимум40calls/40actions/512KiB serializedrequestinput на весь одобренный эксперимент; bytecount отдельно от provider billedtokens. Initial window600seconds including idle/restart; later windows require explicit human-approved operator authorization as documented in jev-flights-pilot.md, each <=600seconds, at most40windows including initial, and never replenish spend/call/action/byte bounds. Decisions-only provider structurally rejects generate_text/visual (notprompt). New live process notauthorizedifpreviousledgerunknown. Official standardprice$0.10/1Minput; bounded conservative request estimate + overhead reserve checked before dispatch; если pricing/usage uncertainty не позволяет соблюсти$0.25 — stopдоspend. Расход model billing отдельно отмечен estimated пока dashboard/usage нет. Никакого auto retry транспорта с неизвестным расходом.

G1 теперь требует APPROVE, не просто завершённоеreview. Native architecture→critic последовательны. Guard fingerprint содержит target-relevant semantics; volatile context отдельно. No-progress excludes regeneratedtokens/UUID, accountsvalues/selected/scroll/loading, boundedhistorytotalbytes. Закрытаятаблица diagnostics: excluded iframe/canvas/shadow/file/password/download/newwindow/namedtarget/oversized/capacity — targetinert and observationincomplete; popup/unknown/context alwaysblock. Если requiredstep в исключённом subtree, не workaround, а BLOCKED.

## Проверка плана

10 октября 2026: после внесения замечаний независимый архитектурный review APPROVE, затем независимый critic APPROVE. Реализация запрошена пользователем и начата. Реальный прогон пока NOT_RUN; завершённость кода не заменяет результат поиска.
