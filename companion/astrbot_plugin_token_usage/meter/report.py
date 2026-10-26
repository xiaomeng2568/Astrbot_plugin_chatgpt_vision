from datetime import datetime, timedelta, timezone
from decimal import Decimal

SHANGHAI = timezone(timedelta(hours=8))


def period_start(period, now=None):
    now = now or datetime.now(SHANGHAI)
    midnight = now.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == '今日':
        return midnight.timestamp()
    if period == '近7天':
        return (midnight - timedelta(days=6)).timestamp()
    return 0


def count(value, known, calls):
    if not known:
        return '未知' if calls else '—'
    return ('≥' if known < calls else '') + f'{value:,}'


def money(values):
    symbols = {'CNY': '¥', 'USD': 'US$', 'EUR': '€'}
    def display(v):
        if 0 < abs(v) < Decimal('0.000001'):
            return '<0.000001' if v > 0 else '>-0.000001'
        return format(v.quantize(Decimal('0.000001')), 'f').rstrip('0').rstrip('.')
    return ' / '.join(symbols.get(k, k + ' ') + display(v) for k, v in sorted(values.items()))


def metrics(report):
    n = report['calls']
    rate = f"{report['cache_ratio'] * 100:.1f}%" if report['cache_ratio'] is not None else '未知'
    if report['costs']:
        fee = money(report['costs'])
    elif n and report['subscription'] == n:
        fee = '订阅用量'
    elif n and report['local'] == n:
        fee = '本地调用'
    else:
        fee = '未配置 / 未知'
    return [count(report['input'], report['input_known'], n),
            count(report['output'], report['output_known'], n), rate, fee]


def plain(report, period, scope_label, dropped=0):
    if not report['calls']:
        return '模型用量统计：当前范围还没有记录。统计从安装启用后开始。'
    m = metrics(report)
    lines = [f'模型用量统计 · {period} · {scope_label}',
             f'输入 Token：{m[0]}（含缓存）', f'输出 Token：{m[1]}',
             f'缓存占比：{m[2]}（按已报告的输入Token）', f'预估费用：{m[3]}',
             f"调用：{report['calls']} 次，其中失败/中断 {report['failed']} 次",
             f"用量已报告：输入 {report['input_known']}/{report['calls']}，缓存 {report['cache_known']}/{report['calls']}"]
    if report['costs']:
        lines.append(f"费用覆盖 {report['priced']}/{report['calls']} 次，仅为已计价部分；非实际账单")
    if report['savings']:
        lines.append('缓存节省估算：' + money(report['savings']))
    if report['subscription']:
        lines.append(f"订阅调用 {report['subscription']} 次，不折算API扣费或剩余额度")
    if report['local']:
        lines.append(f"本地调用 {report['local']} 次，不计算硬件与电费")
    lines.append('按模型统计：')
    for g in report['groups'][:8]:
        prefix = '≥' if g['known'] < g['calls'] else ''
        lines.append(f"{g['provider']} / {g['model']}：{g['calls']}次，输入 {prefix}{g['input']:,}，输出 {prefix}{g['output']:,}")
    if len(report['groups']) > 8:
        lines.append('仅显示前8个模型，可用“模型 名称”筛选。')
    lines.append('北京时间；累计仅含当前保留记录。未知数据不按零计算。')
    if dropped:
        lines.append(f'记录异常或队列丢弃 {dropped} 次，统计可能不完整。')
    return '\n'.join(lines)
