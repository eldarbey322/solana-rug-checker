"""Скоринг: сводим всё в риск 0-100 и одну из пяти категорий."""
from core import Report

# (порог, эмодзи, название, пояснение)
CATEGORIES = [
    (0,  "🟢", "Почти нулевая вероятность скама", "по данным всё чисто, почти 100% не скам"),
    (15, "🟢", "Низкий риск", "явных признаков скама мало — можно торговать, но смотри строку «Риск слива»"),
    (35, "🟡", "Так себе", "есть красные флаги — заходи малой суммой и следи"),
    (55, "🟠", "Вероятный скам", "серьёзные красные флаги — скорее всего сольют"),
    (75, "🔴", "Точно скам", "не трогай"),
]


def category_for(score):
    cat = CATEGORIES[0]
    for c in CATEGORIES:
        if score >= c[0]:
            cat = c
    return cat


def score(r: Report) -> Report:
    pts = 0
    reasons = []

    # Флаги запуска и истории создателя опасны, пока группа ЕЩЁ ДЕРЖИТ токен.
    # Если бандл/создатель/связанные уже вышли — это «грязный запуск в прошлом», а не угроза сейчас.
    from datetime import datetime, timezone
    age_h = ((datetime.now(timezone.utc) - r.launch_ts).total_seconds() / 3600) if r.launch_ts else None
    group_holds = max(r.linked_pct, r.dep_network_hold_pct, r.deployer_pct, r.taxwin_hold_pct)
    launch_exited = r.taxwin_bought_pct >= 10 and r.taxwin_hold_pct < 5
    # видно ли, куда делся сапплай бандла: продали + разложили ≈ сколько купили
    launch_accounted = r.launch_exit_verified or (
        r.taxwin_bought_pct > 0 and r.launch_sold_pct + r.launch_moved_pct >= 0.8 * r.taxwin_bought_pct)
    exit_unseen = launch_exited and not launch_accounted              # «вышли», но куда — не видно
    sync_dump = r.sync_waves >= 3 and r.sync_wallets >= 15
    # бандл не продал, а разложил сапплай по кошелькам (видно по их личной истории)
    spread_out = r.taxwin_bought_pct >= 30 and (r.launch_moved_pct >= 20 or r.launch_moved_wallets >= 50)
    redistributed = spread_out and r.launch_moved_hold_pct >= 5                         # получатели держат — угроза
    spread_sold = spread_out and r.launch_moved_hold_pct < 5 and launch_accounted       # раздали и продали — история
    # продаж бандла не видно, а сапплай раскидан по мелочи при «чистом» топ-10 — та же картина косвенно
    fragmented = (r.taxwin_bought_pct >= 50 and not launch_accounted
                  and r.tail_hold_pct >= 40 and r.top10_eoa_pct <= 25)
    group_trades = (r.wash or {}).get("cluster_vol_pct", 0) >= 30   # группа делает заметную часть объёма
    active = (group_holds >= 5 or (age_h is not None and age_h < 3)
              or r.lp_status in ("held", "held_unknown")
              or group_trades                     # группа всё ещё торгует — значит, она в игре
              or r.tail_risky_est_pct >= 10       # сапплай раздроблен по свежим/связанным мелким кошелькам
              or (r.taxwin_bought_pct >= 30 and exit_unseen)   # куда делся бандл — не видно
              or (sync_dump and r.sync_suspect_ratio >= 0.5)   # связанные сливают волнами
              or redistributed or fragmented)

    def add(p, text):
        nonlocal pts
        pts += p
        reasons.append((p, text))

    # --- контракт ---
    if r.verified is False:
        add(15, "код контракта не верифицирован")
    if r.is_proxy:
        add(25, "контракт обновляемый (прокси) — логику могут поменять в любой момент")
    if r.danger_funcs and r.owner_renounced is not True:
        add(min(30, 10 * len(r.danger_funcs)), f"у владельца есть опасные функции ({len(r.danger_funcs)})")

    # --- создатель ---
    if r.deployer_pct >= 10:
        add(12, f"создатель держит {r.deployer_pct:.1f}%")
    elif r.deployer_pct >= 5:
        add(6, f"создатель держит {r.deployer_pct:.1f}%")
    if r.dep_network_hold_pct >= 20:
        add(15, f"{r.dep_network_holders} кошельков из сети создателя держат {r.dep_network_hold_pct:.0f}%")
    elif r.dep_network_hold_pct >= 5:
        add(8, f"{r.dep_network_holders} кошельков из сети создателя держат {r.dep_network_hold_pct:.0f}%")
    old_tokens = [t for t in r.past_tokens if t["status"] != "свежий"]
    rugs = [t for t in old_tokens if t["status"] == "раг"]
    dead = [t for t in old_tokens if t["status"] in ("раг", "мёртв")]
    tail = "" if active else " (сейчас он и его сеть почти ничего не держат)"
    if rugs:
        t = rugs[0]
        who = "создатель" if t["rel"] == "сам создатель" else "связанный с ним кошелёк"
        add(min(30, 15 * len(rugs)) if active else min(15, 8 * len(rugs)),
            f"раги в прошлом: {len(rugs)} (напр. ${t['symbol']} — {who} слил "
            f"{t['creator_sold_pct']:.0f}%, токен мёртв){tail}")
    if len(old_tokens) >= 3 and len(dead) / len(old_tokens) >= 0.7:
        add(15 if active else 8, f"сеть создателя запускала {len(old_tokens)} токенов, мертвы {len(dead)}")
    elif len(dead) >= 2:
        add(8 if active else 4, f"у сети создателя {len(dead)} мёртвых токенов")
    serial_rugger = len(rugs) >= 2 or (len(old_tokens) >= 4 and len(dead) / len(old_tokens) >= 0.8)

    # --- Solana: mint/freeze authority и ловушки Token-2022 ---
    sol_floor = 0
    if r.chain == "solana":
        if r.sol_mint_auth:
            add(30, "mint authority не отозван — могут допечатать токены")
            sol_floor = max(sol_floor, 55)
        if r.sol_freeze_auth:
            add(30, "freeze authority не отозван — могут заморозить кошелёк, не продашь")
            sol_floor = max(sol_floor, 55)
        for kind, text in r.sol_traps:
            add(10 if kind == "fee" else 20, f"Token-2022: {text}")
            if kind == "hard":
                sol_floor = max(sol_floor, 75)
            elif kind == "mid":
                sol_floor = max(sol_floor, 55)
        if r.feepayer_links >= 3:
            add(10 if active else 3,
                f"{r.feepayer_links} кошельков на запуске: комиссию за их покупки платил другой кошелёк (бандл)"
                + ("" if active else ", но группа уже вышла"))
        if r.feyn and r.feyn.get("level") == "scam":
            add(20, "Feyn: скам")
            sol_floor = max(sol_floor, 55)
        elif r.feyn and r.feyn.get("level") == "high_risk":
            add(8, "Feyn: высокий риск")
        if r.rugcheck and r.rugcheck.get("rugged"):
            add(20, "RugCheck: токен уже рагнут")
            sol_floor = max(sol_floor, 75)

    # --- ликвидность: кто её держит (главный способ «вытащить пул») ---
    lp_floor = 0
    who = "создатель" if r.lp_provider_is_dev else "кошелёк"
    if r.lp_status == "removed":
        add(40, f"ПУЛ УЖЕ ВЫТАЩИЛИ: ликвидность забрали ({who}), денег в пуле почти нет")
    elif r.lp_status == "held":
        add(35, f"пул сделал {who} и ликвидность у него (не сожжена, не заблокирована) — может вытащить пул "
                "в любой момент")
        lp_floor = 55
    elif r.lp_status == "held_unknown":
        add(15, "не удалось проверить, кто держит ликвидность (может, кошелёк)")
        lp_floor = 35
    elif r.lp_status == "locked":
        add(5, "ликвидность в контракте-локере — проверь срок разлока")
    elif r.lp_status == "partial":
        add(15, "из пула уже забирали ликвидность (часть ещё на месте)")
        lp_floor = 35
    elif r.lp_status == "unknown" and r.chain == "solana":
        add(10, "не смог проверить, кто держит ликвидность")
        lp_floor = 35

    # --- окно запуска: налог Pons (Robinhood) или слот создания (Solana) ---
    why_launch = ("так могут только освобождённые от налога кошельки" if r.chain == "robinhood"
                  else "это бандл создателя/снайперов")
    hide_floor = 0
    if redistributed:
        add(25, f"{r.launch_label} купили {r.taxwin_bought_pct:.0f}%, а потом не продали, а разложили "
                f"~{r.launch_moved_pct:.0f}% сапплая по {r.launch_moved_wallets} кошелькам — так прячут концентрацию "
                f"(у получателей сейчас {r.launch_moved_hold_pct:.0f}%)")
        hide_floor = 55
    elif fragmented:
        add(20, f"{r.launch_label} купили {r.taxwin_bought_pct:.0f}%, их продаж не видно, а {r.tail_hold_pct:.0f}% "
                f"сапплая раскидано по мелким кошелькам при топ-10 всего {r.top10_eoa_pct:.0f}% — похоже на раздачу бандла")
        hide_floor = 55
    if spread_sold:
        add(8, f"запуск грязный: бандл купил {r.taxwin_bought_pct:.0f}% и раздал сапплай по "
               f"{r.launch_moved_wallets} кошелькам, но они уже продали (держат {r.launch_moved_hold_pct:.1f}%) — "
               "над ценой этот сапплай больше не висит")
    elif redistributed or fragmented:
        pass                                    # запуск уже учтён правилом выше
    elif exit_unseen:
        add(12 if r.taxwin_bought_pct >= 30 else 6,
            f"{r.launch_label} купили {r.taxwin_bought_pct:.0f}% ({why_launch}); у них осталось "
            f"{r.taxwin_hold_pct:.1f}%, но их продаж не видно — токены могли переехать на другие кошельки")
    elif launch_exited:
        add(6 if r.taxwin_bought_pct >= 30 else 3,
            f"{r.launch_label} купили {r.taxwin_bought_pct:.0f}% ({why_launch}), "
            f"но уже вышли — держат {r.taxwin_hold_pct:.1f}%")
    elif r.taxwin_bought_pct >= 30:
        add(20, f"{r.launch_label} купили {r.taxwin_bought_pct:.0f}% — {why_launch}")
    elif r.taxwin_bought_pct >= 10:
        add(10, f"{r.launch_label} купили {r.taxwin_bought_pct:.0f}%")
    if r.taxwin_hold_pct >= 20:
        add(15, f"эти кошельки до сих пор держат {r.taxwin_hold_pct:.0f}%")

    # --- пузыри ---
    if r.linked_pct >= 40:
        add(30, f"связанные кошельки держат {r.linked_pct:.0f}%")
    elif r.linked_pct >= 20:
        add(18, f"связанные кошельки держат {r.linked_pct:.0f}%")
    elif r.linked_pct >= 10:
        add(8, f"связанные кошельки держат {r.linked_pct:.0f}%")
    if r.fresh_pct >= 30:
        add(12, f"свежие кошельки (созданы прямо перед покупкой) держат {r.fresh_pct:.0f}%")
    elif r.fresh_pct >= 15:
        add(6, f"свежие кошельки держат {r.fresh_pct:.0f}%")
    if r.tail_sampled >= 10 and r.tail_fresh / r.tail_sampled >= 0.4:
        add(12, f"среди мелких холдеров {r.tail_fresh} из {r.tail_sampled} проверенных — свежие одноразовые "
                "кошельки (сапплай раздроблен)")
    if r.tail_risky_est_pct >= 20:
        add(15, f"по выборке ~{r.tail_risky_est_pct:.0f}% сапплая у свежих/связанных мелких кошельков")
    elif r.tail_risky_est_pct >= 10:
        add(8, f"по выборке ~{r.tail_risky_est_pct:.0f}% сапплая у свежих/связанных мелких кошельков")
    # накрутка холдеров раздачей и пылью
    if r.fake_holders_ratio >= 0.3:
        add(12, f"накрутка холдеров: {r.fake_holders} ({100 * r.fake_holders_ratio:.0f}%) получили токен "
                "раздачей и ни разу не покупали")
    elif r.fake_holders_ratio >= 0.15:
        add(6, f"раздача: {r.fake_holders} холдеров ({100 * r.fake_holders_ratio:.0f}%) получили токен бесплатно")
    if r.dust_ratio >= 0.5:
        add(5, f"у {100 * r.dust_ratio:.0f}% холдеров — пыль (число холдеров раздуто)")
    # синхронные продажи волнами
    if sync_dump:
        add(10, f"синхронные продажи: {r.sync_waves} волн, {r.sync_wallets} кошельков продавали в одних и тех "
                f"же блоках ({100 * r.sync_sell_share:.0f}% всех продаж)")
        if r.sync_suspect_ratio >= 0.5:
            add(10, f"{100 * r.sync_suspect_ratio:.0f}% продававших волнами — связанные/свежие/с раздачи")
    if r.top10_eoa_pct >= 35:
        add(12, f"топ-10 кошельков держат {r.top10_eoa_pct:.0f}%")
    elif r.top10_eoa_pct >= 25:
        add(8, f"топ-10 кошельков держат {r.top10_eoa_pct:.0f}%")
    elif r.top10_eoa_pct >= 15:
        add(4, f"топ-10 кошельков держат {r.top10_eoa_pct:.0f}%")
    if r.fresh_count >= 15 and r.fresh_ratio >= 0.5:
        add(12, f"{r.fresh_count} свежих кошельков — {100 * r.fresh_ratio:.0f}% от проверенных")
    elif r.fresh_count >= 10 and r.fresh_ratio >= 0.3:
        add(6, f"{r.fresh_count} свежих кошельков — {100 * r.fresh_ratio:.0f}% от проверенных")

    # --- снайперы ---
    still = r.snipers_hold_pct + r.snipers_moved_pct
    if still >= 30:
        add(15, f"снайперы ещё контролируют {still:.0f}%")
    elif still >= 15:
        add(8, f"снайперы ещё контролируют {still:.0f}%")
    elif still >= 5:
        add(3, f"снайперы ещё контролируют {still:.0f}%")
    if r.snipers_moved_pct >= 5:
        add(10, f"снайперы переложили {r.snipers_moved_pct:.0f}% на другие кошельки — это не выход")
    elif r.snipers_moved_pct >= 2:
        add(5, f"снайперы переложили {r.snipers_moved_pct:.0f}% на другие кошельки")
    if (r.snipers_bought_pct >= 10 and r.snipers_exit_ratio is not None and r.coverage_complete
            and r.snipers_exit_ratio >= 0.9 and still < 5):
        add(-8, "снайперы почти полностью вышли")

    # --- пул ---
    # риск слива: считаем по падению ЦЕНЫ. Команда (связанные+создатель) — главный риск,
    # снайперы — дополнительный (часто это независимые боты)
    if r.team_drop_pct is not None and r.team_pct >= 0.5:
        if r.team_drop_pct >= 70:
            add(20, f"если команда (связанные+создатель) продаст свои {r.team_pct:.0f}%, цена упадёт на ~{r.team_drop_pct:.0f}%")
        elif r.team_drop_pct >= 50:
            add(12, f"если команда продаст свои {r.team_pct:.0f}%, цена упадёт на ~{r.team_drop_pct:.0f}%")
        elif r.team_drop_pct >= 30:
            add(6, f"если команда продаст свои {r.team_pct:.0f}%, цена упадёт на ~{r.team_drop_pct:.0f}%")
    if r.price_drop_pct is not None and r.snipers_left_pct >= 1:
        if r.price_drop_pct >= 70:
            add(10, f"вместе со снайперами ({r.danger_pct:.0f}% сапплая) могут обрушить цену на ~{r.price_drop_pct:.0f}%")
        elif r.price_drop_pct >= 50:
            add(5, f"вместе со снайперами ({r.danger_pct:.0f}% сапплая) могут обрушить цену на ~{r.price_drop_pct:.0f}%")
    # тонкий пул — это волатильность, а не признак скама: показываем в отчёте, но баллов не даём
    if r.pool and r.pool.get("liquidity_usd", 0) < 5000:
        add(10, f"ликвидность всего ${r.pool['liquidity_usd']:,.0f}")

    # --- накрутка объёма (wash trading) ---
    wash_strong = False
    w = r.wash
    if w:
        rt = w["roundtrip_vol_pct"]
        sp = w["size_spread"]
        # «купил-продал» само по себе — так торгуют и обычные флипперы на живом токене.
        # Серьёзно это только вместе с признаками ботов: одинаковые размеры или одна группа.
        botlike = ((sp is not None and sp < 4 and w["trades"] >= 60)
                   or w.get("cluster_vol_pct", 0) >= 30)
        if rt >= 50:
            if botlike:
                add(15, f"{rt:.0f}% объёма — кошельки туда-сюда, и похоже на ботов/одну группу")
            else:
                add(4, f"{rt:.0f}% объёма — кошельки туда-сюда (на живом токене это могут быть флипперы)")
        elif rt >= 25 and botlike:
            add(8, f"{rt:.0f}% объёма — кошельки туда-сюда, похоже на ботов/одну группу")
        if sp is not None and w["trades"] >= 60:
            if sp < 4:
                add(12, f"сделки подозрительно одинакового размера (разброс ×{sp:.1f}, "
                        "у живого токена обычно ×20+)")
            elif sp < 8:
                add(3, f"сделки довольно однообразные по размеру (разброс ×{sp:.1f}; бывает и из-за "
                       "кнопок-пресетов в терминалах)")
        if w["trades"] >= 60 and w["trades_per_wallet"] >= 8:
            add(8, f"{w['trades']} сделок делают всего {w['traders']} кошельков")
        cv = w["interval_cv"]
        if cv is not None and cv < 0.5 and w["trades"] >= 60:
            add(6, "сделки идут слишком ровным ритмом, как по таймеру")
        if w.get("cluster_vol_pct", 0) >= 30:
            add(12, f"{w['cluster_vol_pct']:.0f}% объёма делают связанные кошельки — группа всё ещё "
                    "в игре и управляет ценой (может синхронно слить)")
        wash_strong = (rt >= 50 and botlike) or (sp is not None and sp < 4 and w["trades"] >= 60 and rt >= 20)

    # --- зрелый токен: живёт часами, много настоящих покупателей, команда проверенно вышла ---
    team_out = (r.team_pct < 5 and r.taxwin_hold_pct < 5 and r.launch_moved_hold_pct < 5
                and (launch_accounted or r.taxwin_bought_pct < 10) and r.tail_risky_est_pct < 10
                and r.lp_status not in ("held", "held_unknown", "removed", "partial", "unknown")
                and not r.sol_mint_auth and not r.sol_freeze_auth)
    mature = (r.age_h is not None and r.age_h >= 3 and r.organic_buyers >= 300 and team_out and not active
              and (r.organic_vol_share is None or r.organic_vol_share >= 0.3))
    r.mature = mature
    if mature:
        add(-15, f"зрелый токен: живёт {r.age_h:.0f} ч, {r.organic_buyers} настоящих покупателей, "
                 "команда и бандл почти ничего не держат")
    dirty_launch = (r.taxwin_bought_pct >= 30 or r.snipers_bought_pct >= 60 or r.fake_holders_ratio >= 0.15
                    or bool(rugs))
    r.dirty_launch = dirty_launch
    r.launch_accounted = launch_accounted

    # --- две разные истории: как запускали и что опасно сейчас ---
    story = []
    if r.taxwin_bought_pct >= 30:
        story.append(("бандл купил" if r.chain == "solana" else "кошельки без налога купили")
                     + f" {r.taxwin_bought_pct:.0f}% на запуске")
    if spread_out:
        story.append(f"раздали сапплай по {r.launch_moved_wallets} кошелькам")
    if r.fake_holders_ratio >= 0.15:
        story.append(f"{100 * r.fake_holders_ratio:.0f}% холдеров накручены раздачей")
    if rugs:
        story.append(f"у создателя/его сети {len(rugs)} рага в прошлом")
    r.launch_story = story
    now = []
    if r.team_pct >= 5:
        now.append(f"команда (связанные + создатель) держит {r.team_pct:.0f}%"
                   + (f" → продажа = цена −{r.team_drop_pct:.0f}%" if r.team_drop_pct else ""))
    if r.lp_status == "held":
        now.append(("создатель" if r.lp_provider_is_dev else "кошелёк") + " держит ликвидность — пул могут вытащить")
    if r.lp_status in ("held_unknown", "unknown"):
        now.append("не проверено, заблокирована ли ликвидность")
    if r.lp_status == "partial":
        now.append("из пула уже забирали ликвидность")
    if r.lp_status == "removed":
        now.append("пул уже вытащили")
    if redistributed:
        now.append(f"получатели раздачи бандла держат {r.launch_moved_hold_pct:.0f}%")
    if r.taxwin_bought_pct >= 30 and exit_unseen:
        now.append(f"куда делись {r.taxwin_bought_pct:.0f}% бандла — не видно")
    if fragmented:
        now.append("сапплай, похоже, раскидан бандлом по мелким кошелькам")
    if group_trades:
        now.append(f"одна связанная группа делает {r.wash['cluster_vol_pct']:.0f}% объёма — может слить синхронно")
    if r.tail_risky_est_pct >= 10:
        now.append(f"~{r.tail_risky_est_pct:.0f}% сапплая у свежих/связанных мелких кошельков")
    if sync_dump and r.sync_suspect_ratio >= 0.5:
        now.append("связанные кошельки уже сливают волнами")
    if r.snipers_left_pct >= 15:
        now.append(f"снайперы ещё держат {r.snipers_left_pct:.0f}%")
    if r.sol_mint_auth or r.sol_freeze_auth or any(k == "hard" for k, _ in r.sol_traps):
        now.append("опасные настройки токена (authority / Token-2022)")
    if r.is_proxy or (r.danger_funcs and r.owner_renounced is not True):
        now.append("у владельца контракта опасные права")
    r.now_threats = now

    pts = max(0, pts)

    # --- жёсткие правила "точно скам" ---
    hard = []
    if r.linked_pct >= 50 and (r.team_drain_pct or 0) >= 80:
        hard.append("одна группа держит больше половины и может вынуть почти весь пул")
    if r.taxwin_hold_pct >= 40:
        hard.append("кошельки, купившие без налога на запуске, держат 40%+" if r.chain == "robinhood"
                    else "кошельки из бандла на запуске держат 40%+")
    if r.is_proxy and r.danger_funcs and r.owner_renounced is not True:
        hard.append("обновляемый контракт + опасные функции у живого владельца")
    if r.lp_status == "removed":
        hard.append("ликвидность из пула уже забрали — это раг")
    if hard:
        pts = max(pts, 85)
        for h in hard:
            reasons.insert(0, (0, "⛔ " + h))

    if hide_floor and pts < hide_floor:
        pts = hide_floor
        r.notes.append("бандл с запуска разложил (или, судя по картине, раскидал) сапплай по множеству кошельков — "
                       "реальная концентрация скрыта, поэтому минимум «вероятный скам»")
    if lp_floor and pts < lp_floor:
        pts = lp_floor
        r.notes.append("ликвидность не сожжена и не залочена — её владелец может забрать пул, "
                       "поэтому категорию ниже ставить нельзя")
    if sol_floor and pts < sol_floor:
        pts = sol_floor
        r.notes.append("на Solana есть опасная настройка токена (authority / Token-2022) "
                       "или внешний чекер уже видит скам — категорию ниже ставить нельзя")

    # серийный рагер: пока группа держит токен — минимум «вероятный скам», иначе — «так себе»
    if serial_rugger and active and pts < 55:
        pts = 55
        r.notes.append("создатель или его сеть — серийные рагеры и всё ещё держат токен, "
                       "поэтому минимум «вероятный скам»")
    elif serial_rugger and not active and pts < 35:
        pts = 35
        r.notes.append("создатель — серийный запускатор, но он и его сеть уже вышли из токена: "
                       "запуск был грязный, сейчас главный риск — пустой спрос, поэтому минимум «так себе»")

    # проверили мало кошельков, а на запуске был крупный бандл — «низкий риск» ставить нельзя
    if (r.trace_attempted and r.traced_wallets < 0.7 * r.trace_attempted
            and (r.taxwin_bought_pct >= 30 or r.snipers_bought_pct >= 50) and pts < 35):
        pts = 35
        r.notes.append("кошельков проверено мало, а на запуске был крупный бандл — оценке «низкий риск» "
                       "доверять нельзя, поэтому минимум «так себе»")

    # грязный запуск, а токен ещё не доказал, что живёт сам — не ниже «так себе»
    if dirty_launch and not mature and pts < 35:
        pts = 35
        r.notes.append("запуск был грязный, а признаков, что токен уже живёт сам (часы торговли, сотни "
                       "настоящих покупателей), пока недостаточно — поэтому минимум «так себе»")

    # неверифицированный код: проверить бэкдоры нельзя -> не выше «так себе»
    if r.verified is False and pts < 35:
        pts = 35
        r.notes.append("код не верифицирован — бэкдоры (mint, запрет продаж, налог 100%) "
                       "проверить невозможно, поэтому минимум «так себе»")
    # накрученный объём -> не выше «так себе»
    if wash_strong and pts < 35:
        pts = 35
        r.notes.append("объём накручен ботами — активность на графике в основном фейковая")

    # мало данных — не даём категорию "почти нулевая"
    if pts < 15 and (not r.coverage_complete or not r.pool):
        pts = 15
        r.notes.append("данных недостаточно для категории «почти нулевая» — поставил «низкий»")

    r.score = min(100, pts)
    r.category = category_for(r.score)
    r.reasons = sorted(reasons, key=lambda x: -abs(x[0]) if x[0] else -1000)
    return r
