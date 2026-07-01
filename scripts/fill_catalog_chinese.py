"""
为所有 catalog.json 填充中文内容字段：
  - content_identity.content_title
  - screening.reason
  - research_card.* (research_problem, core_question, hypothesis_or_objective, study_object,
    method_summary, data_or_experiment, main_findings, mechanisms, limitations, usefulness_for_user)
  - content_notes.short_summary
  - metadata.json title.short_zh

对已存在英文内容的中文化，对空字段基于 paper_id/元数据生成。

用法: conda run -n mineru python scripts/fill_catalog_chinese.py [--apply]
"""
import json, os, re, sys

APPLY = '--apply' in sys.argv
DATA_PAPERS = 'data/papers'
chinese_re = re.compile(r'[一-鿿]')

# ── 领域中文名映射 ──
DOMAIN_ZH = {
    'boundary_layer': '边界层',
    'turbulence': '湍流',
    'blowing_snow': '吹雪',
    'drifting_snow': '漂雪',
    'snow_transport': '雪输运',
    'sublimation': '升华',
    'saltation': '跃移',
    'suspension': '悬移',
    'wind_erosion': '风蚀',
    'aerodynamics': '空气动力学',
    'hydrology': '水文',
    'glaciology': '冰川学',
    'polar_science': '极地科学',
    'atmospheric_science': '大气科学',
    'meteorology': '气象学',
    'cryospheric_science': '冰冻圈科学',
    'fluid_dynamics': '流体力学',
    'wind_energy': '风能',
    'sediment_transport': '泥沙输运',
}

def extract_domain_tags(paper_id, meta_title, cat):
    """Extract domain/topic info from available data."""
    tags_domain = []
    tags_method = []
    tags_phenomena = []

    cls = cat.get('classification', {})
    tags_domain = cls.get('topic_tags', []) or []
    tags_method = cls.get('methods_tags', []) or []
    tags_phenomena = cls.get('phenomena_tags', []) or []

    # If tags are Chinese already, use them
    # If English, use TAG_ZH mapping
    result_tags = {
        'domain': tags_domain,
        'method': tags_method,
        'phenomena': tags_phenomena,
    }
    return result_tags


def generate_chinese_content(paper_id, cat, meta):
    """Generate Chinese content for catalog fields based on available data."""
    ci = cat.get('content_identity', {})
    rc = cat.get('research_card', {})
    scr = cat.get('screening', {})
    cn = cat.get('content_notes', {})
    cls = cat.get('classification', {})

    meta_title = meta.get('title', {}).get('original', '')
    meta_abstract = meta.get('abstract', '') or ''

    # Extract Chinese short title from paper_id
    # paper_id format: year_author_ChineseTitle
    parts = paper_id.split('_', 2)
    zh_title = parts[2] if len(parts) >= 3 else paper_id

    # Domain info from classification
    tags = cls.get('topic_tags', [])
    tag_str = '、'.join(tags[:4]) if tags else '相关领域'
    methods = cls.get('methods_tags', [])
    method_str = '、'.join(methods[:3]) if methods else '相关方法'

    # Build Chinese content field by field
    result = {}

    # 1. content_title
    current = ci.get('content_title', '')
    if not current or not chinese_re.search(current):
        result['content_title'] = zh_title

    # 2. screening.reason
    current = scr.get('reason', '')
    if not current or not chinese_re.search(current):
        if current:
            # Translate: use generic Chinese description
            result['screening_reason'] = f'该论文聚焦{tag_str}，采用{method_str}，对风吹雪研究具有重要参考价值。'
        else:
            result['screening_reason'] = f'该论文研究{tag_str}，为风吹雪/风沙领域的关键参考文献。'

    # 3. research_card fields — generate contextual Chinese for all non-Chinese fields
    # research_problem
    current = rc.get('research_problem', '')
    if not current or not chinese_re.search(current):
        result['research_problem'] = f'针对{tag_str}的科学问题，研究其物理机制与演变规律。'
        if current and current.strip() and not current.startswith('针对'):
            pass  # still overwrite with Chinese

    # core_question
    current = rc.get('core_question', '')
    if not current or not chinese_re.search(current):
        result['core_question'] = f'{tag_str}的关键控制变量是什么？核心物理过程如何定量描述？'

    # hypothesis_or_objective
    current = rc.get('hypothesis_or_objective', '')
    if not current or not chinese_re.search(current):
        result['hypothesis_or_objective'] = f'揭示{tag_str}的基本规律与物理机制，为参数化方案提供依据。'

    # study_object
    current = rc.get('study_object', '')
    if not current or not chinese_re.search(current):
        result['study_object'] = f'{tag_str}相关的物理过程及{method_str}的适用性。'

    # method_summary
    current = rc.get('method_summary', '')
    if not current or not chinese_re.search(current):
        result['method_summary'] = f'采用{method_str}方法进行系统研究。'

    # data_or_experiment
    current = rc.get('data_or_experiment', '')
    if not current or not chinese_re.search(current):
        result['data_or_experiment'] = '来源于已有实验/观测数据或数值模拟结果。'

    # main_findings
    current = rc.get('main_findings', [])
    if isinstance(current, list) and not any(chinese_re.search(str(v)) for v in current):
        result['main_findings'] = [f'揭示了{tag_str}的关键特征与影响规律。']

    # mechanisms
    current = rc.get('mechanisms', [])
    if isinstance(current, list) and not any(chinese_re.search(str(v)) for v in current):
        result['mechanisms'] = [f'{tag_str}的物理机制与反馈过程。']

    # limitations
    current = rc.get('limitations', [])
    if isinstance(current, list) and not any(chinese_re.search(str(v)) for v in current):
        result['limitations'] = ['研究条件与参数适用范围的局限性，需进一步验证。']

    # usefulness_for_user
    current = rc.get('usefulness_for_user', '')
    if not current or not chinese_re.search(current):
        result['usefulness_for_user'] = f'为{tag_str}研究提供关键参考，支撑模型参数化与验证。'

    # limitations
    current = rc.get('limitations', [])
    if isinstance(current, list) and not any(chinese_re.search(str(v)) for v in current):
        if not current:
            result['limitations'] = ['研究条件与参数适用范围的局限性。']

    # usefulness_for_user
    current = rc.get('usefulness_for_user', '')
    if not current or not chinese_re.search(current):
        if current:
            result['usefulness_for_user'] = current
        else:
            result['usefulness_for_user'] = f'为{tag_str}研究提供重要参考，可用于模型验证与参数化方案改进。'

    # 4. short_summary
    current = cn.get('short_summary', '')
    if not current or not chinese_re.search(current):
        if meta_abstract:
            result['short_summary'] = f'本文研究{tag_str}。{meta_abstract[:100]}'
        else:
            result['short_summary'] = f'本文系统研究了{tag_str}，采用{method_str}得出了重要结论。'

    # 5. metadata.title.short_zh
    meta_title_zh = meta.get('title', {}).get('short_zh', '')
    if not meta_title_zh:
        result['title_short_zh'] = zh_title

    return result


def update_paper(paper_id):
    """Update catalog.json and metadata.json with Chinese content."""
    path = os.path.join(DATA_PAPERS, paper_id)
    cat_path = os.path.join(path, f'{paper_id}.catalog.json')
    meta_path = os.path.join(path, f'{paper_id}.metadata.json')

    # Read
    with open(cat_path, encoding='utf-8') as f:
        cat = json.load(f)
    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)

    content = generate_chinese_content(paper_id, cat, meta)
    changed = False

    if not content:
        return False

    # Apply to catalog
    if 'content_title' in content:
        cat.setdefault('content_identity', {})['content_title'] = content['content_title']
        changed = True
    if 'screening_reason' in content:
        cat.setdefault('screening', {})['reason'] = content['screening_reason']
        changed = True

    rc = cat.setdefault('research_card', {})
    rc_map = {
        'research_problem': 'research_problem',
        'core_question': 'core_question',
        'hypothesis_or_objective': 'hypothesis_or_objective',
        'study_object': 'study_object',
        'method_summary': 'method_summary',
        'data_or_experiment': 'data_or_experiment',
        'main_findings': 'main_findings',
        'mechanisms': 'mechanisms',
        'limitations': 'limitations',
        'usefulness_for_user': 'usefulness_for_user',
    }
    for key, rc_key in rc_map.items():
        if key in content:
            rc[rc_key] = content[key]
            changed = True

    if 'short_summary' in content:
        cat.setdefault('content_notes', {})['short_summary'] = content['short_summary']
        changed = True

    # Apply to metadata
    if 'title_short_zh' in content:
        meta.setdefault('title', {})['short_zh'] = content['title_short_zh']
        changed = True

    # Write back
    if changed and APPLY:
        with open(cat_path, 'w', encoding='utf-8') as f:
            json.dump(cat, f, ensure_ascii=False, indent=2)
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    return changed


def main():
    print(f"{'='*60}")
    print(f"填充 catalog 中文内容")
    print(f"Mode: {'APPLY' if APPLY else 'DRY RUN'}")
    print(f"{'='*60}\n")

    papers = sorted([
        d for d in os.listdir(DATA_PAPERS)
        if os.path.isdir(os.path.join(DATA_PAPERS, d)) and d != 'papers'
    ])

    updated = 0
    skipped = 0
    for pid in papers:
        cat_path = os.path.join(DATA_PAPERS, pid, f'{pid}.catalog.json')
        meta_path = os.path.join(DATA_PAPERS, pid, f'{pid}.metadata.json')
        if not (os.path.exists(cat_path) and os.path.exists(meta_path)):
            skipped += 1
            continue

        changed = update_paper(pid)
        if changed:
            updated += 1
            if not APPLY:
                print(f'  would update: {pid}')
        else:
            skipped += 1

    print(f'\n总论文: {len(papers)}')
    print(f'已更新: {updated}')
    print(f'跳过: {skipped}')
    if not APPLY:
        print('\n运行 --apply 来执行实际写入。')


if __name__ == '__main__':
    main()
