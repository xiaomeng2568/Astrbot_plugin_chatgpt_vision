"""Local Pillow card. No remote renderer, font download, HTML, or image cache."""
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from .report import metrics, money

FONTS = [Path('C:/Windows/Fonts/msyh.ttc'), Path('C:/Windows/Fonts/simhei.ttf'),
         Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
         Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc')]


def render(report, period, scope_label, sample=False):
    font = next((p for p in FONTS if p.is_file()), None)
    if font is None:
        raise RuntimeError('Chinese font unavailable')
    def f(size):
        return ImageFont.truetype(str(font), size)
    image = Image.new('RGB', (1120, 960), '#171717')
    d = ImageDraw.Draw(image)
    d.text((48, 34), '模型用量统计', font=f(42), fill='#f5f5f5')
    d.text((48, 103), f'{period} · {scope_label} · 北京时间', font=f(22), fill='#aaaaaa')
    values = metrics(report)
    labels = ['输入 Token（含缓存）', '输出 Token', '缓存Token占比', '预估费用（已计价部分）']
    for index, (label, value) in enumerate(zip(labels, values)):
        x = 48 + (index % 2) * 522
        y = 160 + (index // 2) * 180
        d.rounded_rectangle((x, y, x + 502, y + 160), radius=22, fill='#101010')
        d.text((x + 22, y + 18), label, font=f(22), fill='#a9a9a9')
        value_font = f(40 if len(value) < 18 else 26)
        # Monetary totals may contain multiple currencies; clip with an ellipsis.
        while d.textlength(value, font=value_font) > 455 and len(value) > 1:
            value = value[:-2] + '…'
        d.text((x + 22, y + 65), value, font=value_font,
               fill='#46ce8c' if index == 2 else '#ffffff')
    d.line((48, 550, 1072, 550), fill='#303030', width=2)
    d.text((48, 571), f"调用 {report['calls']} 次 · 失败/中断 {report['failed']} 次", font=f(23), fill='#eeeeee')
    d.text((48, 612), f"缓存数据覆盖 {report['cache_known']}/{report['calls']} 次 · 费用覆盖 {report['priced']}/{report['calls']} 次",
           font=f(20), fill='#aaaaaa')
    for index, group in enumerate(report['groups'][:4]):
        line = f"{group['model']} · {group['provider']} · {group['calls']} 次"
        while d.textlength(line, font=f(21)) > 1024:
            line = line[:-2] + '…'
        d.text((48, 662 + index * 43), line, font=f(21), fill='#d0d0d0')
    if report['savings']:
        d.text((48, 829), '缓存节省估算：' + money(report['savings']), font=f(19), fill='#46ce8c')
    if sample:
        footer = '示例数据 · 非真实用量 · 非实际账单'
    elif report['subscription']:
        footer = '含订阅调用：不折算API费用或剩余额度'
    else:
        footer = '费用仅为估算；未知字段不按零计算'
    d.text((48, 867), footer, font=f(21), fill='#9d9d9d')
    d.text((48, 908), '从插件启用后统计；累计仅含当前保留数据', font=f(18), fill='#777777')
    output = BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()
