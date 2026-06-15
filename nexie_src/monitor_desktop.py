# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
import os
import sys
import time

# 强制 UTF-8 输出
sys.stdout.reconfigure(encoding='utf-8')

DESKTOP = r'C:\Users\21036\Desktop'
today = time.strftime('%Y-%m-%d')
END_TIME = time.mktime(time.strptime(f'{today} 13:30:00', '%Y-%m-%d %H:%M:%S'))

LAPTOP_CONTENT = """========================================
  笔记本电脑销售专区 — 精选推荐
========================================

【热销型号 TOP 5】

1. 联想 ThinkPad X1 Carbon Gen 11
   处理器：Intel Core i7-1365U
   内存：16GB LPDDR5
   硬盘：512GB SSD NVMe
   屏幕：14" 2.8K OLED (2880×1800)
   重量：1.12kg
   价格：¥12,999
   亮点：军规认证、顶级键盘、超轻便携

2. 华为 MateBook X Pro 2024
   处理器：Intel Core Ultra 9 185H
   内存：32GB LPDDR5x
   硬盘：1TB SSD
   屏幕：14.2" 3.1K OLED 触控屏
   重量：1.26kg
   价格：¥14,999
   亮点：超级终端、隔空操控、3:2生产力屏

3. Apple MacBook Pro 14 (M3 Pro)
   芯片：Apple M3 Pro (11核CPU + 14核GPU)
   内存：18GB 统一内存
   硬盘：512GB SSD
   屏幕：14.2" Liquid Retina XDR
   重量：1.61kg
   价格：¥16,999
   亮点：续航怪兽(17h)、mini-LED屏、静音无风扇

4. 华硕 ROG 幻16 Air
   处理器：AMD Ryzen AI 9 HX 370
   内存：32GB LPDDR5x-7500
   硬盘：1TB SSD
   屏幕：16" 2.5K OLED 240Hz
   显卡：RTX 4060 8GB
   重量：1.85kg
   价格：¥13,999
   亮点：轻薄游戏、240Hz高刷、创意设计

5. 荣耀 MagicBook Pro 16
   处理器：Intel Core Ultra 7 155H
   内存：16GB LPDDR5x
   硬盘：1TB SSD
   屏幕：16" 3K IPS 165Hz
   重量：1.79kg
   价格：¥8,999
   亮点：OS Turbo加持、性价比之王、跨屏协同

========================================

【按需求选购指南】

▌学生/办公入门 (3000-6000元)
  • 联想 小新Pro 14 — ¥5,499
  • 红米 RedmiBook Pro 15 — ¥4,299
  • 惠普 战66 六代 — ¥4,899
  • 华硕 无畏15i — ¥3,999

▌商务精英 (6000-12000元)
  • ThinkPad X1 Carbon — ¥12,999
  • Dell XPS 13 Plus — ¥10,999
  • HP EliteBook 840 G10 — ¥8,499
  • 华为 MateBook 14s — ¥7,299

▌内容创作/设计 (10000-20000元)
  • MacBook Pro 16 M3 Max — ¥19,999
  • 华硕 ProArt 创16 — ¥15,999
  • Dell Precision 5680 — ¥18,499
  • 联想 Legion 9i — ¥16,999

▌游戏电竞 (7000-30000元)
  • ROG 枪神8 Plus — ¥12,999
  • 拯救者 Y9000P 2024 — ¥9,499
  • Alienware m18 R2 — ¥29,999
  • 宏碁 掠夺者·擎 Neo — ¥7,999

========================================

【2024年购机趋势】

1. AI PC元年：Intel Core Ultra与AMD Ryzen AI处理器
   集成NPU，本地运行大模型成为现实

2. OLED普及：高端笔记本全面转向OLED屏幕，
   对比度、色域远超IPS面板

3. 内存大升级：16GB起步、32GB成主流，
   LPDDR5x频率突破7500MHz

4. 快充生态：100W+ Type-C快充普及，
   30分钟充50%已成标配

5. 轻薄全能化：2kg以下也能塞进独显，
   游戏本与轻薄本边界日益模糊

========================================

【促销活动】

🔥 618年中大促火热进行中！
  • 全场笔记本满3000减300
  • 学生认证享额外9.5折
  • 以旧换新最高补贴¥1000
  • 12期免息分期
  • 赠送笔记本内胆包+无线鼠标

📞 销售热线：400-888-XXXX
🌐 官网：https://www.laptopstore.example.com
🏬 线下体验店：全国300+门店，欢迎到店体验

========================================
  数据更新时间：2024年6月
========================================
"""

print(f"[Monitor] 开始监测: {DESKTOP}")
print(f"[Monitor] 截止时间: {today} 13:30")

existing = set()
for f in os.listdir(DESKTOP):
    fp = os.path.join(DESKTOP, f)
    if f.endswith('.txt') and os.path.isfile(fp):
        existing.add(f)

print(f"[Monitor] 初始txt: {len(existing)} 个")

while time.time() < END_TIME:
    try:
        current = set()
        for f in os.listdir(DESKTOP):
            fp = os.path.join(DESKTOP, f)
            if f.endswith('.txt') and os.path.isfile(fp):
                current.add(f)
        
        new_files = current - existing
        for nf in new_files:
            fp = os.path.join(DESKTOP, nf)
            try:
                with open(fp, 'w', encoding='utf-8') as f:
                    f.write(LAPTOP_CONTENT)
                print(f"[Monitor] [OK] 已填充: {nf}")
                existing.add(nf)
            except Exception as e:
                print(f"[Monitor] [FAIL] 写入 {nf}: {e}")
        
    except Exception as e:
        print(f"[Monitor] [ERR] {e}")
    
    time.sleep(10)

print("[Monitor] 监测结束 (13:30)")
