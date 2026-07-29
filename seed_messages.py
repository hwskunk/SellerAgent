"""
快速填充 32 条测试消息（16 轮对话），用于验证 Summary Buffer 总结触发。

每次添加后自动检查是否需要总结，脚本会显示总结是否触发。
"""
import sys; sys.path.insert(0, ".")
from src.memory import add_message, maybe_summarize, get_summary, get_recent_messages

THREAD = "demo_test"
ROUNDS = [
    ("你好，我想了解一下电脑", "您好！我们有轻薄本、游戏本、商务本，您主要什么用途？"),
    ("主要办公用，偶尔看视频", "明白了，办公+影音的话推荐轻薄本，预算大概多少？"),
    ("3000左右吧，不要太贵的", "3000以内可选的轻薄本有不少，比如华为MateBook D、联想小新、惠普战66等"),
    ("华为那个怎么样？", "MateBook D系列性价比不错，屏幕好、续航长，适合办公。缺点是接口少点"),
    ("联想小新呢？", "小新系列性能释放好，散热强，但稍微重一点。价格比华为便宜一些"),
    ("我比较看重屏幕和续航", "那华为MateBook 14可能更适合，2K屏幕+12小时续航，就是价格会超一点预算"),
    ("超多少？", "MateBook 14大概3500左右，超了500。如果严格3000以内可以看MateBook D SE，屏幕也不错"),
    ("那就看MateBook D SE吧", "好选择！这个配置日常办公足够用了。您对颜色和内存有偏好吗？"),
    ("银色，16G内存就行", "好的，银色16G版本3000以内可以拿下。需要我帮您对比一下其他品牌同价位的吗？"),
    ("都对比谁？", "同价位的荣耀MagicBook X也值得看，屏幕稍差但性能略好。还有宏碁非凡Go，接口更丰富"),
    ("荣耀和华为哪个好？", "华为胜在屏幕和做工，荣耀性能释放好一点但屏幕一般。办公的话华为更合适"),
    ("那就决定华为了，什么时候有优惠？", "双十一和618是大促节点，平时月末也常有闪购。我帮您关注一下价格～"),
    ("行，帮我留意一下", "没问题！我再记一下：您是小王，预算3000，要银色16G的华为MateBook D SE，办公影音用。对吗？"),
    ("对的", "好的，都记下了！有优惠第一时间通知您。还有其他问题可以随时问我～"),
    ("谢谢，再问一下售后怎么样", "华为笔记本全国联保2年，主要部件保修3年。售后网点覆盖很全，一线城市都有官方售后"),
    ("好的，就这些了", "不客气！有需要随时找我。祝您早日买到心仪的电脑～"),
]

for i, (user_msg, ai_msg) in enumerate(ROUNDS, 1):
    add_message(THREAD, "user", user_msg)
    add_message(THREAD, "assistant", ai_msg)
    total = i * 2
    triggered = maybe_summarize(THREAD)
    note = " >>> SUMMARY TRIGGERED!" if triggered else ""
    print(f"[{total:2d}条消息/{i:2d}轮] 已添加{note}")

print()
summary = get_summary(THREAD)
msgs, total = get_recent_messages(THREAD)
print(f"=== 记忆状态 ===")
print(f"总消息数: {total}")
print(f"最近窗口: {len(msgs)} 条（最近 10 轮）")
print(f"是否有总结: {summary is not None}")
if summary:
    print(f"总结 ({len(summary)} 字符):")
    print(summary)
