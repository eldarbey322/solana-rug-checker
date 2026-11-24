"""Превращаем Report в сообщение для Telegram (HTML).

Формат: сначала по каждой проверке одна строка ✅/⚠️/❌, в конце итоговая категория и причины.
"""
from html import escape

import config
from core import Report, short

OK, WARN, BAD, NA = "✅", "⚠️", "❌", "▫️"


def _link(addr, chain="robinhood"):
    if chain == "solana":
        return f'<a href="https://solscan.io/account/{addr}">{short(addr)}</a>'
    return f'<a href="{config.EXPLORER_URL}/address/{addr}">{short(addr)}</a>'


def _worst(*marks):
    for m in (BAD, WARN, OK):
        if m in marks:
            return m
    return NA


# ---------- отдельные проверки: каждая возвращает (значок, текст) ----------

def check_contract(r):
    if r.chain == "solana":
        marks = [m for m, _ in r.sol_checks]
        bad = [t for m, t in r.sol_checks if m != OK]
        if not bad:
            return OK, "; ".join(t for _, t in r.sol_checks)
        return _worst(*marks), "; ".join(bad)
    if r.verified is False:
        return BAD, "НЕ верифицирован — код скрыт, бэкдоры проверить нельзя"
    bits, marks = [], []
    if r.is_proxy:
        bits.append("обновляемый (прокси) — код могут поменять")
        marks.append(BAD)
    if r.danger_funcs:
        if r.owner_renounced is True:
            bits.append(f"есть опасные функции ({len(r.danger_funcs)}), но владелец отказался")
            marks.append(OK)
        else:
            bits.append(f"у владельца есть опасные функции: "
                        + ", ".join(f.split("—")[-1].strip().replace("может ", "")
                                    for f in r.danger_funcs[:3]))
            marks.append(BAD if len(r.danger_funcs) >= 2 else WARN)
    if not bits:
        return OK, "верифицирован, не прокси, опасных функций нет"
    return _worst(*marks), "верифицирован, но " + "; ".join(bits)


def check_pool(r):
    if not r.pool:
        return NA, "не найден (токен ещё на кривой или не проиндексирован)"
    liq = r.pool.get("liquidity_usd") or 0
    where = " (ещё на кривой pump.fun)" if (r.pool.get("dex") or "").lower() == "pumpfun" else ""
    if liq < 5000:
        return WARN, f"ликвидность всего ${liq:,.0f}{where}"
    return OK, f"ликвидность ${liq:,.0f}{where}"


def check_lp(r):
    if r.lp_status is None:
        return None
    sol = r.chain == "solana"
    link = (lambda a: _link(a, r.chain)) if sol else _link
    who = "создатель" if r.lp_provider_is_dev else f"кошелёк {link(r.lp_provider)}" if r.lp_provider else "?"
    note = f" ({escape(r.lp_note)})" if r.lp_note else ""
    st = r.lp_status
    if st == "launchpad":
        if sol:
            return OK, ("пул сделал лаунчпад при выходе с кривой, а не кошелёк "
                        "(pump.fun и подобные сжигают/блокируют LP)" + note)
        return OK, "добавил лаунчпад, а не кошелёк (на Pons она блокируется навсегда при выходе с кривой)"
    by = f"добавил {who}, " if r.lp_provider else ""
    if st == "burned":
        return OK, f"{by}{'LP сожжён' if sol else 'позиция сожжена'} — пул не вытащить{note}"
    if st == "locked":
        where = f" в программе-локере {link(r.lp_holder)}" if r.lp_holder else " заблокирован"
        return WARN, (f"{by}{'LP' if sol else 'позиция'}{where} — проверь срок разлока{note}")
    if st == "held":
        share = f" ({r.lp_eoa_share:.0f}% ликвидности)" if r.lp_source == "pool" and r.lp_eoa_share else ""
        whose = "создателя" if r.lp_provider_is_dev else f"кошелька {link(r.lp_provider)}"
        what = ("позиция (без LP-токена)" if r.lp_kind == "position"
                else "LP" if sol else "позиция")
        return BAD, f"пул сделал не лаунчпад: {what} у {whose}{share} — может вытащить пул в любой момент{note}"
    if st == "held_unknown":
        if sol:
            return WARN, ("пул сделал не лаунчпад, а заблокирована ли ликвидность — проверить не смог; "
                          "проверь «LP burned/locked» на GMGN/RugCheck" + note)
        if r.lp_source == "pool":
            return WARN, "не смог узнать владельца позиции (сбой запроса) — проверь вручную или повтори позже"
        return WARN, f"добавил {who}; залочена ли позиция, проверить не смог"
    if st == "partial":
        left = (r.pool or {}).get("liquidity_usd") or 0
        return WARN, (f"из пула уже забирали ликвидность, но в нём ещё ${left:,.0f} — "
                      "следи, не заберут ли остальное")
    if st == "removed":
        left = (r.pool or {}).get("liquidity_usd")
        left_txt = f", в пуле осталось ${left:,.0f}" if left is not None else ", пул не найден"
        by = f" ({who})" if r.lp_provider else ""
        return BAD, f"ПУЛ ВЫТАЩИЛИ — ликвидность забрали{by}{left_txt}"
    if sol:
        return WARN, "не смог проверить ликвидность (не дошёл до создания пула) — проверь «LP burned» вручную"
    return NA, "не нашёл, кто добавил ликвидность (пул не Uniswap v4?)"


def check_launch(r):
    if not r.launch_known:
        return NA, "не дошёл до создания токена, запуск не проверял"
    sol = r.chain == "solana"
    payer = (f"; у {r.feepayer_links} кошельков на запуске комиссию платил другой кошелёк"
             if sol and r.feepayer_links else "")
    if not r.taxwin_count:
        return (WARN if sol and r.feepayer_links >= 3 else OK), f"{r.launch_label} никто не покупал{payer}"
    txt = f"{r.taxwin_count} кошельков купили {r.taxwin_bought_pct:.0f}% {r.launch_label}"
    if r.taxwin_bought_pct >= 10 and (r.launch_moved_pct >= 20 or r.launch_moved_wallets >= 50):
        spread = (f" — продали {r.launch_sold_pct:.0f}%, а ~{r.launch_moved_pct:.0f}% разложили по "
                  f"{r.launch_moved_wallets} кошелькам")
        if r.launch_moved_hold_pct < 5 and r.launch_accounted:
            return WARN, txt + spread + (f"; получатели уже продали (держат {r.launch_moved_hold_pct:.1f}%) — "
                                         f"запуск грязный, но этот сапплай над ценой больше не висит{payer}")
        return BAD, txt + spread + f" — и у получателей сейчас {r.launch_moved_hold_pct:.1f}%{payer}"
    if (r.taxwin_bought_pct >= 50 and not r.launch_accounted and r.tail_hold_pct >= 40
            and r.top10_eoa_pct <= 25):
        return BAD, txt + (f" — их продаж не видно, а {r.tail_hold_pct:.0f}% сапплая раскидано по мелким кошелькам "
                           f"при топ-10 {r.top10_eoa_pct:.0f}%: похоже, бандл раздал сапплай{payer}")
    if r.taxwin_bought_pct >= 10 and r.taxwin_hold_pct < 5 and not r.launch_accounted:
        mark = BAD if r.taxwin_bought_pct >= 30 else WARN
        return mark, txt + (f" — у них осталось {r.taxwin_hold_pct:.1f}%, но их продаж не видно (середина "
                            f"истории пропущена): токены могли переехать на другие кошельки{payer}")
    if r.taxwin_bought_pct >= 10 and r.taxwin_hold_pct < 5:
        what = "это был бандл, но он уже вышел" if sol else \
            "это были освобождённые от налога кошельки, но они уже вышли"
        return WARN, txt + f" — {what} (держат {r.taxwin_hold_pct:.1f}%){payer}"
    if r.taxwin_bought_pct >= 30:
        why = "так покупают только освобождённые от налога" if not sol else "это бандл"
        return BAD, txt + f" — {why}; ещё держат {r.taxwin_hold_pct:.0f}%{payer}"
    if r.taxwin_bought_pct >= 10 or (sol and r.feepayer_links >= 3):
        return WARN, txt + f", держат {r.taxwin_hold_pct:.0f}%{payer}"
    return OK, txt + f" — немного{payer}"


def check_snipers(r):
    if not r.launch_known:
        return NA, "не проверял (длинная история)"
    if not r.snipers_count:
        return OK, "не найдены"
    still = r.snipers_hold_pct + r.snipers_moved_pct
    exit_pct = 100 * (r.snipers_exit_ratio or 0)
    bought = (f"купили {r.snipers_bought_pct:.0f}% сапплая" if r.snipers_bought_pct <= 100 else
              f"оборот покупок {r.snipers_bought_pct:.0f}% сапплая (покупали-продавали по кругу)")
    head = f"{r.snipers_count} шт, {bought}"
    moved = (f", ещё {r.snipers_moved_pct:.0f}% переложили на другие кошельки (это не выход)"
             if r.snipers_moved_pct >= 1 else "")
    if (still < 5 and exit_pct >= 90 and not r.coverage_complete and r.launch_accounted
            and r.snipers_bought_pct <= r.taxwin_bought_pct + 5):
        return OK, (f"{head}, держат {r.snipers_hold_pct:.1f}% — в основном это тот же бандл, "
                    "куда он дел токены — в строке «Запуск»")
    if still < 5 and exit_pct >= 90 and not r.coverage_complete:
        return (WARN if r.snipers_bought_pct >= 50 else OK), (
            f"{head}, у них осталось {r.snipers_hold_pct:.1f}% — продали или переложили, "
            "точно не видно (середина истории пропущена)")
    if still < 5 and exit_pct >= 90:
        return OK, f"{head}, почти всё продали ({exit_pct:.0f}% купленного), держат {r.snipers_hold_pct:.1f}%"
    mark = BAD if still >= 30 else WARN if still >= 5 else OK
    if r.snipers_moved_pct >= 5:
        mark = _worst(mark, WARN)
    return mark, f"{head}, продали {exit_pct:.0f}% купленного, сейчас держат {r.snipers_hold_pct:.1f}%{moved}"


def check_linked(r):
    big = [c for c in r.clusters if c["size"] >= 3 or c["hold_pct"] >= 1]
    if not big:
        return OK, f"связей не нашёл (проверил {r.traced_wallets} кошельков)"
    c = big[0]
    tag = " + создатель" if c["with_deployer"] else ""
    why = "; ".join(c["reasons"][:2])
    txt = (f"держат {r.linked_pct:.0f}% всего; крупнейшая группа — {c['size']} кошельков{tag}, "
           f"{c['hold_pct']:.0f}% ({why})")
    if len(big) > 1:
        txt += f"; групп всего: {len(big)}"
    mark = BAD if r.linked_pct >= 40 else WARN if r.linked_pct >= 10 else OK
    return mark, escape(txt)


def check_fresh(r):
    if not r.traced_wallets:
        return NA, "не проверял"
    txt = (f"{r.fresh_count} из {r.traced_wallets} ({100 * r.fresh_ratio:.0f}%), "
           f"держат {r.fresh_pct:.0f}%")
    mark = OK
    if (r.fresh_count >= 15 and r.fresh_ratio >= 0.5) or r.fresh_pct >= 30:
        mark, txt = BAD, txt + " — слишком много"
    elif (r.fresh_count >= 10 and r.fresh_ratio >= 0.3) or r.fresh_pct >= 15:
        mark = WARN
    if r.tail_sampled:
        txt += (f"\n   мелкие холдеры (вне топа, у них {r.tail_hold_pct:.0f}% сапплая): проверил {r.tail_sampled}, "
                f"свежих {r.tail_fresh}, из связанных групп {r.tail_linked}")
        if r.tail_risky_est_pct >= 1:
            txt += f" → ~{r.tail_risky_est_pct:.0f}% сапплая у подозрительной мелочи"
        if r.tail_risky_est_pct >= 20 or (r.tail_sampled >= 10 and r.tail_fresh / r.tail_sampled >= 0.4):
            mark = BAD
        elif r.tail_risky_est_pct >= 10:
            mark = _worst(mark, WARN)
    return mark, txt


def check_top10(r):
    p = r.top10_eoa_pct
    mark = BAD if p >= 35 else WARN if p >= 15 else OK
    return mark, f"держат {p:.0f}% (без пула и сожжённых)"


def check_holders(r):
    parts, mark = [], OK
    if r.airdrop_senders:
        parts.append(f"раздача: {r.airdrop_senders} кошел. разослали токен на {r.airdrop_recipients} адресов, "
                     f"{r.fake_holders} холдеров ({100 * r.fake_holders_ratio:.0f}%) получили его бесплатно и не покупали")
        mark = BAD if r.fake_holders_ratio >= 0.3 else WARN if r.fake_holders_ratio >= 0.15 else OK
    if r.dust_ratio >= 0.3:
        parts.append(f"у {100 * r.dust_ratio:.0f}% холдеров пыль")
        if r.dust_ratio >= 0.5:
            mark = _worst(mark, WARN)
    if not parts:
        return OK, f"{r.holders_count} холдеров, раздач и пыли не видно"
    return mark, f"{r.holders_count} холдеров; " + "; ".join(parts)


def check_sync(r):
    if r.sync_waves >= 3 and r.sync_wallets >= 15:
        txt = (f"{r.sync_waves} волн: {r.sync_wallets} кошельков продавали в одних и тех же блоках "
               f"({100 * r.sync_sell_share:.0f}% всех продаж)")
        if r.sync_suspect_ratio >= 0.5:
            return BAD, txt + f"; {100 * r.sync_suspect_ratio:.0f}% из них — связанные/свежие/с раздачи"
        return WARN, txt
    if r.sync_waves:
        return OK, f"синхронных сливов почти нет ({r.sync_waves} волн)"
    return OK, "синхронных сливов не видно"


def check_volume(r):
    w = r.wash
    if not w:
        return NA, "мало сделок для анализа"
    rt, sp = w["roundtrip_vol_pct"], w["size_spread"]
    flags = []
    if rt >= 25:
        flags.append(f"{rt:.0f}% объёма — кошельки туда-сюда")
    if sp is not None and w["trades"] >= 60 and sp < 8:
        flags.append(f"сделки одного размера (разброс ×{sp:.1f})")
    if w["trades"] >= 60 and w["trades_per_wallet"] >= 8:
        flags.append(f"{w['trades']} сделок всего от {w['traders']} кошельков")
    if w["interval_cv"] is not None and w["interval_cv"] < 0.5 and w["trades"] >= 60:
        flags.append("ровный ритм, как по таймеру")
    if (w["tiny_share"] or 0) < 0.03 and (w["whale_share"] or 0) < 0.03 and flags:
        flags.append("нет ни мелочи, ни китов")
    if w.get("cluster_vol_pct", 0) >= 30:
        flags.append(f"{w['cluster_vol_pct']:.0f}% объёма последних сделок делает одна группа связанных "
                     "кошельков — она управляет ценой и может синхронно слить")
    if not flags:
        sp_txt = f", разные размеры (×{sp:.0f})" if sp else ""
        return OK, f"живой — {w['trades']} сделок от {w['traders']} кошельков{sp_txt}"
    strong = (rt >= 50 or (sp is not None and sp < 4 and w["trades"] >= 60)
              or w.get("cluster_vol_pct", 0) >= 40)
    return (BAD if strong else WARN), "похоже на накрутку: " + ", ".join(flags)


def check_deployer(r):
    if not r.deployer:
        return NA, "не определил"
    txt = f"{_link(r.deployer, r.chain)} держит {r.deployer_pct:.1f}%"
    mark = BAD if r.deployer_pct >= 10 else WARN if r.deployer_pct >= 5 else OK
    if r.dep_funder:
        txt += (", деньги получил с биржи/сервиса" if r.dep_funder_exchange
                else f", спонсор {_link(r.dep_funder, r.chain)}")
    return mark, txt


def check_dep_network(r):
    if not r.deployer:
        return NA, "не проверял"
    n = len(r.dep_network)
    if not n:
        return OK, "связанных кошельков не нашёл"
    txt = f"{n} связанных кошельков"
    if r.dep_network_holders:
        txt += f", из них {r.dep_network_holders} держат этот токен — {r.dep_network_hold_pct:.1f}%"
        mark = BAD if r.dep_network_hold_pct >= 20 else WARN if r.dep_network_hold_pct >= 5 else OK
    else:
        txt += ", этот токен не держат"
        mark = OK
    return mark, txt


def check_past(r):
    if not r.deployer:
        return NA, "не проверял"
    past = r.past_tokens
    if not past:
        plus = " (смотрел последние 50 транзакций)" if r.deployer_history_partial else ""
        return OK, "других запусков у создателя и его сети не нашёл" + plus
    old = [t for t in past if t["status"] != "свежий"]
    rugs = [t for t in old if t["status"] == "раг"]
    dead = [t for t in old if t["status"] == "мёртв"]
    alive = [t for t in old if t["status"] == "жив"]
    txt = f"{len(past)} шт: рагов {len(rugs)}, мёртвых {len(dead)}, живых {len(alive)}"
    mark = BAD if rugs or (len(old) >= 3 and (len(rugs) + len(dead)) / len(old) >= 0.7) \
        else WARN if len(rugs) + len(dead) >= 2 else OK
    lines = [txt]
    icon = {"раг": "☠️", "мёртв": "💀", "жив": "🟢", "свежий": "🆕"}
    for t in (rugs + dead + alive + [x for x in past if x["status"] == "свежий"])[:5]:
        who = "создатель" if t["rel"] == "сам создатель" else f"{short(t['creator'])} ({t['rel']})"
        extra = f", его создатель слил {t['creator_sold_pct']:.0f}%" if t["creator_sold_pct"] >= 1 else ""
        lines.append(f"   {icon[t['status']]} ${escape(t['symbol'])} — {t['status']}, "
                     f"ликв. ${t['liquidity_usd']:,.0f}{extra}; запустил {escape(who)}")
    return mark, "\n".join(lines)


def check_dump(r):
    if r.price_drop_pct is None and r.team_drop_pct is None:
        if r.pool and r.danger_pct < 0.1:
            return OK, "опасные кошельки токен не держат — сливать некому"
        return NA, "не посчитать (нет пула)"
    cur = ((r.pool or {}).get("quote_symbol") or ("SOL" if r.chain == "solana" else "ETH")).upper()
    lines = []
    team_drop = r.team_drop_pct or 0
    if r.team_pct >= 0.5:
        lines.append(f"команда (связанные + создатель) держит {r.team_pct:.1f}% — если продаст: "
                     f"цена −{team_drop:.0f}%, из пула уйдёт ~{r.team_drain_pct:.0f}% {cur} (~${r.team_drain_usd:,.0f})")
    else:
        lines.append("у команды (связанные + создатель) почти ничего нет")
    all_drop = r.price_drop_pct or 0
    if r.snipers_left_pct >= 1:
        lines.append(f"   вместе со снайперами ({r.danger_pct:.1f}% сапплая, могут быть независимы): цена −{all_drop:.0f}%")
    if r.impact_1pct is not None and r.impact_1pct >= 5:
        lines.append(f"   ⚠️ пул тонкий: продажа даже 1% сапплая — цена −{r.impact_1pct:.0f}%")
    mark = BAD if team_drop >= 50 else WARN if (team_drop >= 30 or all_drop >= 50 or (r.impact_1pct or 0) >= 10) else OK
    return mark, "\n".join(lines)


def check_second(r):
    if r.chain != "solana":
        return None
    parts, marks = [], []
    if r.rugcheck:
        rc = r.rugcheck
        if rc.get("rugged"):
            parts.append("RugCheck: РАГНУТ")
            marks.append(BAD)
        else:
            risks = ", ".join(escape(str(n)) for _, n in rc.get("risks") or [])
            lp_txt = (f", LP заблокирована {rc['lp_locked_pct']:.0f}%" if rc.get("lp_locked_pct") is not None else "")
            parts.append(f"RugCheck: score {rc.get('score')}{lp_txt}" + (f" ({risks})" if risks else ""))
            marks.append(WARN if any(l == "danger" for l, _ in rc.get("risks") or []) else OK)
    if r.feyn and r.feyn.get("level"):
        lvl = r.feyn["level"]
        parts.append(f"Feyn: {escape(lvl)}")
        marks.append(BAD if lvl == "scam" else WARN if lvl in ("high_risk", "caution") else OK)
    if not parts:
        return NA, "RugCheck/Feyn недоступны"
    return _worst(*marks), "; ".join(parts)


CHECKS = [
    ("Контракт", check_contract),
    ("Пул", check_pool),
    ("Ликвидность", check_lp),
    ("Запуск", check_launch),
    ("Снайперы", check_snipers),
    ("Связанные кошельки", check_linked),
    ("Свежие кошельки", check_fresh),
    ("Топ-10", check_top10),
    ("Холдеры", check_holders),
    ("Объём", check_volume),
    ("Синхронные продажи", check_sync),
    ("Создатель", check_deployer),
    ("Сеть создателя", check_dep_network),
    ("Прошлые запуски", check_past),
    ("Риск слива", check_dump),
    ("Второе мнение", check_second),
]


def render(r: Report) -> str:
    if r.chain == "solana":
        net = "◎ Solana" + (f" · {r.launchpad}" if r.launchpad else "")
        url = f"https://solscan.io/token/{r.token}"
    else:
        net = "🪶 Robinhood Chain"
        url = f"{config.EXPLORER_URL}/token/{r.token}"
    L = [
        f'🔎 <b>{escape(r.name)} (${escape(r.symbol)})</b> — {net}',
        f'<a href="{url}">{r.token}</a>',
        "",
    ]
    for title, fn in CHECKS:
        res = fn(r)
        if res is None:
            continue
        mark, text = res
        L.append(f"{mark} <b>{title}:</b> {text}")

    L.append("")
    if r.launch_story:
        L.append("📜 <b>Как запускали:</b> грязно — " + escape("; ".join(r.launch_story)))
    else:
        L.append("📜 <b>Как запускали:</b> явных махинаций не видно")
    if r.now_threats:
        L.append("⚡ <b>Что опасно сейчас:</b> " + escape("; ".join(r.now_threats)))
    else:
        tail = (" — токен уже живёт сам" if r.mature else "")
        L.append("⚡ <b>Что опасно сейчас:</b> крупных угроз не видно: команда и бандл почти ничего "
                 f"не держат{tail}. Главный риск — обычная волатильность")

    emoji, cat, desc = r.category[1], r.category[2], r.category[3]
    L += ["━━━━━━━━━━━━━━",
          f"{emoji} <b>ИТОГ: {cat.upper()}</b> — риск {r.score}/100",
          f"<i>{desc}</i>"]

    main = [t for p, t in r.reasons if p > 0 or t.startswith("⛔")][:4]
    good = [t for p, t in r.reasons if p < 0]
    if main:
        L += ["", "<b>Главные причины:</b>"] + [f"• {escape(t)}" for t in main]
    if good:
        L += [f"• в плюс: {escape(t)}" for t in good]
    if r.notes:
        L += [""] + [f"ℹ️ {escape(n)}" for n in r.notes]
    L += ["", "<i>Это эвристика, а не гарантия. «Чисто» = ничего не нашли.</i>"]

    text = "\n".join(L)
    return text if len(text) < 4000 else text[:3990] + "…"
