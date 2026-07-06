# Legacy only. Predates metadata v2.0 / catalog v3.0 / paper_index v2.0.
# One-shot hardcoded script writing legacy fields short_zh and content_identity.content_title.
# Do not run against the active library. Kept for historical reference.
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').parent))
from src.utils.atomic_io import atomic_write_json

raw = Path('data/paper_raw')
updates = [{"folder":"1973_jackson_untitled","content":{"short_zh":"低丘湍流边界层流动","content_title":"低矮山丘上的湍流风场","mechanisms":["湍流边界层内区与外区的耦合流动机制","地形诱导的风速摄动与切应力响应","丘陵曲率对近地表风速的加速效应"],"limitations":["理论仅适用于小曲率二维丘陵地形，排除了陡坡和悬崖等复杂地形","假设中性层结条件，未考虑大气稳定度对流动的影响","来流假设为对数律廓线，推广至更复杂来流条件需谨慎"]}},{"folder":"1975_deaves_untitled","content":{"short_zh":"山坡风场数值模拟","content_title":"风过山坡：一种数值方法","mechanisms":["大气边界层气流经过山坡的加速效应","湍流应力与平均流场之间的涡粘性闭合","有限差分法求解二维Navier-Stokes方程"],"limitations":["仅考虑二维流动，未计入三维绕流对山顶风速的削减效应","无法准确处理流动分离，分离区结果不可靠","仅适用于细长山坡（高度/长度比小），陡峭地形不适用"]}},{"folder":"1975_taylor_untitled","content":{"short_zh":"粗糙波动壁湍流边界层数值解","content_title":"固定粗糙波动表面上湍流边界层流动的数值解","mechanisms":["粗糙波动表面对深湍流边界层流动的扰动机制","波幅、波形和表面粗糙度对速度剖面和应力分布的影响","流线曲率对湍流结构的修正效应"],"limitations":["数值方法在水平流速为负时不稳定，仅能处理固定波面情形，不适用于自由发展波动","模型假设波列完全规则且理想化，与实际河床沙波或砾石波的随机分布有差距","未考虑有限水深和自由表面效应，模型适用水深范围受限"]}},{"folder":"1981_carruthers_untitled","content":{"short_zh":"中等坡度丘陵上空气流","content_title":"中等坡度丘陵上空的气流","mechanisms":["逆温层盖边界层流动","地形诱导的风速加速效应","大气稳定度引起的流动不对称性"],"limitations":["线性化涡度方程配合非线性边界条件引入近似误差","辐射边界条件未考虑上传播模态的非线性相互作用","模型假定各层内风切变为零且稳定度为常数"]}},{"folder":"1983_mason_untitled","content":{"short_zh":"连续山脊-山谷大气边界层流动","content_title":"近二维连续山脊-山谷地形上的大气流动","mechanisms":["陡坡背风侧气流分离","周期性地形对边界层湍流的调制","山谷风场减速与山顶加速效应"],"limitations":["仅局限于近中性层结条件，缺少稳定与不稳定层结数据","风向覆盖不完整（350°-10°区间无数据）","系留气球定向测量因强烈湍流导致数据可靠性不足"]}},{"folder":"1984_hunt_untitled","content":{"short_zh":"低丘陵湍流剪切流理论","content_title":"低丘陵上的湍流剪切流","mechanisms":["湍流边界层摄动分析与渐近匹配","来流切变对丘陵绕流速度场和切应力场的影响","非线性惯性效应对丘陵流动的修正"],"limitations":["仅适用于中性层结大气，未考虑层结浮力效应","仅分析低坡度丘陵（H/L << 1），高坡度情形不适用","湍流封闭采用混合长假设，对复杂湍流刻画精度有限"]}},{"folder":"1984_mason_untitled","content":{"short_zh":"孤立山丘流动与湍流测量","content_title":"中等坡度孤立山丘上的流动与湍流测量及预测","mechanisms":["线性理论预测地形引起的风速加速效应","湍流边界层流经孤立山丘时的结构变化","背风侧流动分离与尾流动力学"],"limitations":["小尺度地形不规则性对观测数据质量的限制","线性理论在背风侧流动预测中存在显著偏差","Jackson-Hunt线性理论被应用于超出其严格有效范围的工况"]}},{"folder":"1987_unknown_untitled","content":{"short_zh":"山谷边界层流动分离","content_title":"山谷上方边界层流动的分离特征","mechanisms":["线性化层流和湍流边界层模型对山谷分离的预测","地形诱导的流动分离机制（表面切应力符号变化）","稳定层结和山谷形态对分离临界高度的影响"],"limitations":["线性化模型仅适用于小坡度地形（H/L<<1），对陡峭地形不适用","仅讨论二维情形，未考虑三维山谷流动的复杂效应","湍流模型采用混合长闭合方案，在分离回流区存在固有局限"]}},{"folder":"1989_schumann_untitled","content":{"short_zh":"上坡边界层大涡模拟","content_title":"上坡边界层的大涡模拟","mechanisms":["浮力驱动的斜坡上坡热力环流","切变与浮力共同作用的湍流生成机制","坡度影响下的相干结构（滚轴涡旋）演化"],"limitations":["未考虑科里奥利力，仅限非旋转系统","地表粗糙度仅以单一粗糙度长度参数化，过于简化","仅考虑白天均匀加热条件，未涵盖夜间情形"]}},{"folder":"1992_dornbrack_untitled","content":{"short_zh":"起伏地形对流边界层LES","content_title":"起伏地形上湍流对流流动的数值模拟","mechanisms":["大涡模拟对流边界层湍流","地形诱导的滚涡及相干结构","表面波压力阻力与摩擦阻力机制"],"limitations":["地形简化为理想化正弦波，未考虑真实复杂性","忽略科里奥利力和顶部逆温层卷夹过程","粗网格未解析近地面内层，仅适用对流主导条件"]}},{"folder":"1992_raupach_untitled","content":{"short_zh":"粗糙表面阻力与分配","content_title":"粗糙表面上的阻力和阻力分配","mechanisms":["粗糙元尾流遮蔽导致的基底应力衰减效应","粗糙元尾流的随机叠加与相互作用","湍流边界层中粗糙元空气动力学阻力的尺度规律"],"limitations":["理论基于两个启发式假设，缺乏对粗糙冠层内湍流的详细动力学处理","分析局限于完全粗糙流动条件（hu_*/ν>55），不适用于低雷诺数过渡区","高粗糙密度（λ>0.1-0.3）时尾流不可再视为独立个体，理论精度下降"]}},{"folder":"1993_wood_untitled","content":{"short_zh":"中性湍流山丘压力阻力","content_title":"中性湍流流经丘陵地形时产生的压力阻力","mechanisms":["中性湍流经丘陵地形时由迎风坡与背风坡压力不对称产生的拖曳","湍流闭合模型阶数（混合长 vs. 二阶闭合）对压力阻力计算结果的影响","有效粗糙长度作为亚网格地形拖曳参数化方法的物理基础"],"limitations":["三维模拟因计算代价仅采用1.5阶湍流闭合，未使用二阶闭合模型","仅考虑中性层结条件，未涉及稳定或不稳定层结的影响","陡坡和中等坡度情形采用启发式扩展而非严格解析推导"]}},{"folder":"1995_wood_untitled","content":{"short_zh":"山丘湍流分离临界坡度","content_title":"中性湍流边界层山丘流动中分离的起始","mechanisms":["山丘背风坡逆压梯度驱动近地表涡量积累并引发流动分离","表面应力消失作为平均流分离的判据","线性扰动理论结合非线性应力表达式估算临界坡度"],"limitations":["仅研究平均流分离而非更具物理意义的间歇性分离（后者需涡解析模型）","数值网格分辨率不足以精细解析分离点附近的应力奇异性","仅针对中性层结条件，未考虑稳定或对流边界层情形"]}},{"folder":"1999_sugiura_untitled","content":{"short_zh":"吹雪雪粒溅射函数测定","content_title":"风吹雪中雪粒反弹系数和溅射数量的风洞测量：溅射函数的确定","mechanisms":["雪粒撞击雪面后的垂直与水平反弹机制","雪粒碰撞引起的多层溅射机制","跃移过程中颗粒与雪床的动量交换动力学"],"limitations":["仅分析碰撞前后极短轨迹以避免风、重力、静电等干扰，可能引入轨迹采样偏差","冲击速度分析范围局限于1.0-2.5 m/s，冲击角局限于5-15度，摩擦风速仅设三种固定值","未考虑静电力对雪粒轨迹的显著影响"]}},{"folder":"2001_doorschot_untitled","content":{"short_zh":"平衡态跃移质量通量模型","content_title":"平衡态跃移：质量通量、空气动力学夹带及颗粒特性的影响","mechanisms":["空气动力学夹带（aerodynamic entrainment）：气流直接拾取静止颗粒","颗粒反弹（rebound）：跃移颗粒撞击地表后弹回继续运动","溅射起跳（splash/ejection）：撞击能量使地表其他颗粒跃入气流"],"limitations":["模型方程中忽略了湍流脉动对颗粒轨迹的影响","仅与文献中已报告的雪跃移实测数据对比，缺乏独立实验验证","采用简化的线性反弹能量关系，未细致模拟碰撞角度与摩擦力"]}},{"folder":"2007_unknown_untitled","content":{"short_zh":"三维陡坡植被覆盖湍流LES","content_title":"植被覆盖三维陡坡上湍流边界层的大涡模拟分析","mechanisms":["植被反馈强迫耦合机制：植被运动方程与Navier-Stokes方程联立求解，实现植被冠层湍流的数值表达","陡坡流动分离与再附着机制：三维陡坡背风面分离区湍流结构及植被对尾流非定常特性的影响","曲面植被冠层湍流输运机制：植被顶部相干结构引起的高湍流强度和冠层内部湍流抑制"],"limitations":["仅验证了单一坡度(32°)的正弦曲线山丘模型，未探讨不同坡度或山形对结果的影响","植被模型采用简化的反馈强迫方法，未考虑植被形态细节（如叶片分布、茎秆刚度等）的差异","缺乏现场实测或不同风洞数据的交叉验证，结论的普适性有待进一步检验"]}}]
count = 0
for u in updates:
    folder = raw / u['folder']
    meta_files = list(folder.glob('*.metadata.json'))
    if meta_files and u['content'].get('short_zh'):
        meta = json.loads(meta_files[0].read_text(encoding='utf-8'))
        meta.setdefault('title', {})['short_zh'] = u['content']['short_zh']
        atomic_write_json(meta_files[0], meta, indent=2)
    cat_files = list(folder.glob('*.catalog.json'))
    if cat_files:
        cat = json.loads(cat_files[0].read_text(encoding='utf-8'))
        if u['content'].get('content_title'):
            cat.setdefault('content_identity', {})['content_title'] = u['content']['content_title']
        if u['content'].get('mechanisms'):
            cat.setdefault('research_card', {})['mechanisms'] = u['content']['mechanisms']
        if u['content'].get('limitations'):
            cat.setdefault('research_card', {})['limitations'] = u['content']['limitations']
        atomic_write_json(cat_files[0], cat, indent=2)
    st_path = folder / '.import_status.json'
    if st_path.exists():
        st = json.loads(st_path.read_text(encoding='utf-8'))
        st['status'] = 'catalog_ready'
        atomic_write_json(st_path, st, indent=2)
    count += 1
    print(f'{u["folder"]}: updated')
print(f'Applied: {count}')
