"""
将 data/papers 中英文命名的 paper 目录批量重命名为规范格式：
  年份_作者小写_中文短标题

同时：
  - 更新 catalog.json（paper_id, source_id, 标签改用中文）
  - 更新 metadata.json（source_id）
  - 更新 paper_number_ledger.json（folder_name, folder_path）
  - catalog v2.0 的分类标签（topic_tags 等）中文化

用法: conda run -n mineru python scripts/rename_english_papers.py [--dry-run] [--apply]
"""
import json, os, re, shutil, sys, copy

DRY_RUN = '--dry-run' in sys.argv
APPLY = '--apply' in sys.argv
DATA_PAPERS = 'data/papers'
LEDGER_PATH = 'data/catalog/paper_number_ledger.json'

# ── 中英文标签映射词典 ──────────────────────────────────
TAG_ZH = {
    # 通用方法
    'large-eddy simulation': '大涡模拟', 'large_eddy_simulation': '大涡模拟',
    'large-eddy simulations': '大涡模拟',
    'numerical simulation': '数值模拟', 'numerical modelling': '数值模拟',
    'numerical model': '数值模型', 'numerical modeling': '数值模拟',
    'wind tunnel': '风洞实验', 'wind tunnel experiments': '风洞实验',
    'field observation': '野外观测', 'field measurements': '野外测量',
    'field measurement': '野外测量',
    'remote sensing': '遥感', 'satellite remote sensing': '卫星遥感',
    'lidar': '激光雷达', 'radar remote sensing': '雷达遥感',
    'satellite lidar': '卫星激光雷达',
    'meteorological profiling': '气象廓线观测',
    'PIV': '粒子图像测速',
    'random flight model': '随机飞行模型',
    'RANS': 'RANS模拟',
    'spectrum': '谱分析',
    'coherent structures': '相干结构',
    'turbulence modelling': '湍流模拟',
    'two-phase flow': '两相流',
    'particle-resolved DNS': '粒子解析直接数值模拟',
    'rapid distortion theory': '快速畸变理论',
    'stochastic model': '随机模型',
    'statistics': '统计分析',
    'parameterization': '参数化',

    # 风沙/吹雪核心
    'blowing snow': '吹雪', 'drifting snow': '漂雪', 'snow drift': '雪漂移',
    'snow transport': '雪输运', 'snow drifting': '风吹雪',
    'snow redistribution': '雪再分布', 'snow redistribution': '雪再分布',
    'snow accumulation': '积雪', 'snow deposition': '雪沉积',
    'snow deposition': '雪沉积', 'snow cover': '雪盖',
    'snow load': '雪荷载', 'snow streamers': '雪流线',
    'snow particle counter': '雪粒子计数器', 'SPC': '雪粒子计数器',
    'snow_particle_counter': '雪粒子计数器',
    'saltation': '跃移', 'suspension': '悬移',
    'snow saltation': '雪跃移', 'snow suspension': '雪悬移',
    'snow transport rate': '雪输运率',
    'snow mass flux': '雪质量通量',
    'snow concentration': '雪浓度',
    'snow hardness': '雪硬度',
    'snow ablation': '雪消融',
    'snow_atmosphere coupling': '雪气耦合',
    'threshold wind speed': '阈值风速',
    'particle speed': '粒子速度',
    'particle size': '粒径',
    'particle shape': '粒子形态',
    'particle velocity': '粒子速度',
    'particle inertia': '粒子惯性',
    'size distribution': '粒径分布', 'size_distribution': '粒径分布',
    'number flux': '数量通量',
    'mass flux': '质量通量',
    'mass loss': '质量损失',
    'transport rate': '输运率',

    # 升华
    'sublimation': '升华',
    'drifting snow sublimation': '漂雪升华',
    'windborne-snow sublimation': '风吹雪升华',
    'snow sublimation': '雪升华',
    'moisture transport': '水汽输送',
    'moisture budget': '水汽收支',
    'relative humidity': '相对湿度',
    'saturation': '饱和',
    'thermodynamic feedback': '热力学反馈',
    'thermodynamic feedbacks': '热力学反馈',
    'temperature sensitivity': '温度敏感性',
    'vertical moisture diffusion': '垂直水汽扩散',
    'turbulent diffusion': '湍流扩散',

    # 极地
    'Antarctica': '南极', 'antarctic': '南极',
    'Mizuho station': '瑞穗站', 'Mizuho_station': '瑞穗站',
    'Queen Maud Land': '毛德皇后地',
    'polar regions': '极区',
    'polar science': '极地科学',

    # 地形/山地
    'complex terrain': '复杂地形', 'complex topography': '复杂地形',
    'alpine terrain': '高山地形',
    'hills': '山丘', 'hill flows': '山丘流动',
    'hill': '山丘',
    'flow over hills': '山丘流动',
    'mountain ridge': '山脊',
    'topographic forcing': '地形强迫',
    'topographic drag': '地形阻力',
    'form drag': '形态阻力',
    'non-separated sheltering': '非分离遮蔽',
    'roughness model': '粗糙度模型',
    'roughness length': '粗糙度长度',
    'roughness density': '粗糙度密度',
    'roughness parameter': '粗糙度参数',
    'surface roughness': '地表粗糙度',
    'effective roughness length': '有效粗糙度长度',
    'rough surface': '粗糙表面',
    'displacement height': '位移高度',
    'drag coefficient': '阻力系数',
    'drag partition': '阻力分配',
    'drag model': '阻力模型',
    'effective frontal area index': '有效迎风面积指数',
    'obstacle arrays': '障碍物阵列',
    'sinusoidal topography': '正弦地形',
    'fractal surface': '分形表面',
    'fractal geometry': '分形几何',
    'scale analysis': '尺度分析',
    'subgrid snow distribution': '次网格雪分布',
    'snow depth statistics': '雪深统计',
    'terrain-based parameter': '地形参数',

    # 边界层/湍流
    'boundary layer': '边界层',
    'atmospheric boundary layer': '大气边界层',
    'turbulent boundary layer': '湍流边界层',
    'boundary-layer flow': '边界层流动',
    'boundary layer parameterization': '边界层参数化',
    'stable boundary layer': '稳定边界层',
    'internal gravity waves': '内重力波',
    'stable stratification': '稳定层结',
    'nocturnal boundary layer': '夜间边界层',
    'stable internal boundary layer': '稳定内边界层',
    'boundary layer decoupling': '边界层解耦',
    'advective heat transport': '平流热输送',
    'turbulence': '湍流', 'turbulence modulation': '湍流调制',
    'turbulent flow': '湍流流动',
    'turbulent structures': '湍流结构',
    'turbulent diffusion': '湍流扩散',
    'flow separation': '流动分离', 'reversed flow': '回流',
    'convection': '对流',
    'ABL': '大气边界层',
    'friction velocity': '摩擦速度',
    'momentum flux': '动量通量',
    'wind profile': '风廓线',
    'wind profile modification': '风廓线修正',
    'wind speed': '风速',
    'wind resource': '风能资源',
    'wind energy': '风能',
    'wind erosion': '风蚀',
    'wind transport': '风输运',
    'wind crust': '风壳',
    'wind packing': '风压实', 'wind_packing': '风压实',
    'wind turbine wake': '风力机尾流', 'wind_turbine_wake': '风力机尾流',
    'self-similarity': '自相似性',
    'turbulence modulation': '湍流调制',
    'particle-laden flow': '颗粒两相流',

    # 水文/冰川
    'glacier mass balance': '冰川物质平衡',
    'degree-day model': '度日模型',
    'glacier meteorology': '冰川气象',
    'albedo': '反照率',
    'radiation modeling': '辐射模拟',
    'water budget': '水分收支',
    'Mackenzie Basin': 'Mackenzie流域',
    'Swiss glaciers': '瑞士冰川',
    'climate change': '气候变化',
    'hydrological model': '水文模型',
    'distributed hydrological model': '分布式水文模型',
    'rainfall runoff': '降雨径流',
    'river flow': '河流流量',
    'real-time forecasting': '实时预报',
    'Kalman filter': '卡尔曼滤波',
    'NWSRFS': '美国国家天气局河流预报系统',
    'Cohocton': 'Cohocton河',
    'snow distribution model': '雪分布模型',
    'snow transport model': '雪输运模型',
    'snow cover model': '雪盖模型',
    'snow covered area': '积雪面积', 'SCA parameterization': '积雪面积参数化',
    'energy balance': '能量平衡',
    'mass_energy_balance': '质量能量平衡',
    'spatial variability': '空间变异性',
    'preferential deposition': '优先沉积',
    'preferential_deposition': '优先沉积',
    'snow_deposition': '雪沉积',
    'orographic precipitation': '地形降水',
    'slope effect': '坡面效应',

    # 晶体/形态
    'snow crystals': '雪晶',
    'snow_fragmentation': '雪晶破碎',
    'fragmentation': '破碎',
    'electrostatic charge': '静电荷',
    'electric field': '电场',
    'electrostatic force': '静电力',
    'charge-to-mass ratio': '荷质比',
    'repose angle': '休止角',
    'barchan dunes': '新月形沙丘',
    'aeolian_transport': '风沙输送',

    # 模型/方法
    'SnowTran-3D': 'SnowTran-3D模型',
    'Alpine3D': 'Alpine3D模型',
    'CRYOWRF': 'CRYOWRF模型',
    'WRF': 'WRF模式',
    'SNOWPACK': 'SNOWPACK模型',
    'PIEKTUK model': 'PIEKTUK模型',
    'WaSiM-ETH': 'WaSiM-ETH模型',
    'Thorpe-Mason model': 'Thorpe-Mason模型', 'Thorpe_Mason model': 'Thorpe-Mason模型',
    'Lagrangian stochastic': '拉格朗日随机模型',
    'Lagrangian stochastic model': '拉格朗日随机模型',
    'CFD': 'CFD模拟',
    'wall functions': '壁面函数',
    'horizontal homogeneity': '水平均匀性',
    'sand grain roughness': '砂粒粗糙度',
    'Bagnold theory': 'Bagnold理论',
    'bulk model': '体相模型',
    'LES': '大涡模拟',
    'flow separation': '流动分离',
    'wind energy assessment': '风能评估',
    'gravity-driven flows': '重力驱动流动',

    # 粒子的输运
    'saltating particles': '跃移颗粒',
    'particle residence time': '粒子驻留时间',
    'erodible bed': '可蚀床面',
    'erosion flux': '侵蚀通量',
    'deposition flux': '沉积通量',
    'splash entrainment': '溅起夹带',
    'stochastic model': '随机模型',
    'criterion': '判据',
    'wind-snow coupling': '风雪耦合',
    'wind_snow coupling': '风雪耦合',
    'wind profile modification': '风廓线修正',
    'particle-laden flow': '颗粒两相流',
    'drag model': '阻力模型',
}

def translate_tags(tags):
    """将英文标签列表转为中文"""
    result = []
    for t in tags:
        t_lower = t.lower()
        if t_lower in TAG_ZH:
            result.append(TAG_ZH[t_lower])
        elif '_' in t:
            # Try replacing underscores with spaces
            t_with_spaces = t.replace('_', ' ')
            if t_with_spaces.lower() in TAG_ZH:
                result.append(TAG_ZH[t_with_spaces.lower()])
            else:
                result.append(t)  # keep original
        else:
            result.append(t)  # keep original
    return result


def sanitize_paper_id(pid):
    """清理 paper_id，替换非法字符为 _"""
    import re
    pid = re.sub(r"[\\/:*?\"<>|.\s()+\-&#%!;=@~`\[\]{}',‐–—]+", "_", pid)
    pid = re.sub(r"\s+", "_", pid.strip())
    pid = pid.strip("_")
    return pid


def extract_author_from_dir(dirname):
    """从目录名提取作者姓氏（小写）"""
    parts = dirname.split('_', 2)
    if len(parts) >= 2:
        return parts[1].lower()
    return ''


def make_chinese_title(dirname, meta, cat):
    """从现有数据生成中文短标题（4-10字）"""
    # 优先用已有中文 summary 提炼
    cat_schema = cat.get('schema_version', '')
    summary = ''
    keywords = []

    if cat_schema == '2.0':
        # v2 catalog - get English tags
        tags = cat.get('classification', {}).get('topic_tags', [])
        content_title = cat.get('content_identity', {}).get('content_title', '')
        # Translate tags to Chinese
        zh_tags = translate_tags(tags)
        # Pick the most specific zh tags
        for t in zh_tags:
            if t not in keywords and len(t) <= 8:
                keywords.append(t)
        summary = content_title
    else:
        # Legacy catalog - get Chinese summary and keywords
        summary = cat.get('summary', '') or ''
        keywords = cat.get('keywords', []) or []

    # Try to generate a short Chinese title from available data
    title_orig = meta.get('title', {}).get('original', '')

    # Strategy: use keywords/summary to pick the best Chinese identifier
    # For legacy catalogs, keywords are already Chinese
    # For v2 catalogs, keywords have been translated above

    if keywords:
        # Pick the most characteristic keyword (not too generic)
        generic_zh = {'吹雪', '漂雪', '雪', '湍流', '边界层', '数值模拟', '风洞实验',
                      '大涡模拟', '南极', '复杂地形', '跃移', '悬移', '升华', '雪输运'}
        specific = [k for k in keywords if k not in generic_zh]
        if specific:
            # Use specific + domain keyword
            domain_kw = [k for k in keywords if k in generic_zh][:1]
            title_parts = specific[:2] + domain_kw
            if len(''.join(title_parts)) <= 12:
                return ''.join(title_parts)
            return specific[0] if len(specific[0]) <= 10 else specific[0][:8]
        # Fall back to generic keywords
        if keywords:
            kw_joined = ''.join(keywords[:2])
            if len(kw_joined) <= 12:
                return kw_joined
            return keywords[0][:8]

    # Last resort: use original title keywords
    # Extract meaningful words
    words = re.findall(r'[A-Z][a-z]+', title_orig)
    key_words = [w.lower() for w in words if len(w) > 3 and w.lower() not in
                 {'from', 'that', 'this', 'with', 'over', 'into', 'model', 'study', 'effect',
                  'part', 'flow', 'new', 'method', 'based', 'using', 'simulation', 'measurement'}]
    if key_words:
        return '_'.join(key_words[:3])

    return 'untitled'


# ── 主重命名映射 ──────────────────────────────────────
# 手动核定每篇的中文标题，确保准确性和简洁性
PAPER_RENAME_MAP = {
    "1955_Dryden_Fifty_Years_of_Boundary_Layer_Theory_and_Experiment":
        ("1955", "dryden", "边界层五十年回顾"),
    "1969_Lettau_Note_on_Aerodynamic_Roughness_Parameter_Estimation_on_the_Basis_of_Roughness_Element_Description":
        ("1969", "lettau", "粗糙元空气动力学粗糙度"),
    "1974_Taylor_A_model_of_atmospheric_boundary_layer_flow_above_an_isolated_two_dimensional_hill__an_example_of_flow_above_gentle_topography":
        ("1974", "taylor", "二维山丘边界层流动"),
    "1976_Kind_A_critical_examination_of_the_requirements_for_model_simulation_of_wind_induced_erosiondeposition_phenomena_such_as_snow_drifting":
        ("1976", "kind", "风致侵蚀沉积风洞相似性"),
    "1978_Kobayashi_Snow_Transport_by_Katabatic_Winds_in_Mizuho_Camp_Area__East_Antarctica":
        ("1978", "kobayashi", "南极katabatic风吹雪输送"),
    "1979_Mason_Flow_over_an_isolated_hill_of_moderate_slope":
        ("1979", "mason", "孤立山坡流动"),
    "1980_Dyunin_Redistribution_of_snow_in_the_mountains_under_the_effect_of_heavy_snow_storms":
        ("1980", "dyunin", "山地暴雪再分布"),
    "1980_Fohn_Snow_Transport_Over_Mountain_Crests":
        ("1980", "fohn", "山脊雪输运"),
    "1980_Kitanidis_Real_time_forecasting_with_a_conceptual_hydrologic_model_2__Applications_and_results":
        ("1980", "kitanidis", "概念水文模型实时预报"),
    "1981_Kikuchi_A_wind_tunnel_study_of_the_aerodynamic_roughness_associated_with_drifting_snow":
        ("1981", "kikuchi", "风吹雪粗糙度风洞实验"),
    "1982_Schmidt_Vertical_profiles_of_wind_speed__snow_concentration__and_humidity_in_blowing_snow":
        ("1982", "schmidt", "风吹雪垂直廓线"),
    "1985_Takahashi_Characteristics_of_Drifting_Snow_at_Mizuho_Station__Antarctica":
        ("1985", "takahashi", "南极瑞穗站漂雪特征"),
    "1986_Schmidt_Transport_rate_of_drifting_snow_and_the_mean_wind_speed_profile":
        ("1986", "schmidt", "吹雪输运率与风速廓线"),
    "1989_Taylor_On_the_parameterization_of_drag_over_small_scale_topography_in_neutrally_stratified_boundary_layer_flow":
        ("1989", "taylor", "小地形阻力参数化"),
    "1993_Belcher_The_drag_on_an_undulating_surface_induced_by_the_flow_of_a_turbulent_boundary_layer":
        ("1993", "belcher", "起伏地表湍流边界层阻力"),
    "1998_Gauer_Blowing_and_drifting_snow_in_Alpine_terrain_numerical_simulation_and_related_field_measurements":
        ("1998", "gauer", "高山风吹雪数值模拟"),
    "1998_Liston_A_snow_transport_model_for_complex_terrain":
        ("1998", "liston", "复杂地形雪输运模型"),
    "1998_Macdonald_An_improved_method_for_the_estimation_of_surface_roughness_of_obstacle_arrays":
        ("1998", "macdonald", "障碍物阵列粗糙度估算"),
    "1998_Naaim_Numerical_simulation_of_drifting_snow_erosion_and_deposition_models":
        ("1998", "naaim", "风吹雪侵蚀沉积模拟"),
    "1998_Sugiura_Measurements_of_snow_mass_flux_and_transport_rate_at_different_particle_diameters_in_drifting_snow":
        ("1998", "sugiura", "吹雪质量通量粒径测量"),
    "1998_Taylor_The_Thermodynamic_Effects_of_Sublimating__Blowing_Snow_in_the_Atmospheric_Boundary_Layer":
        ("1998", "taylor", "吹雪升华热力学效应"),
    "1999_Dery_A_Bulk_Blowing_Snow_Model":
        ("1999", "dery", "吹雪体相模型"),
    "1999_Schmidt_Electrostatic_Force_in_Blowing_Snow":
        ("1999", "schmidt", "吹雪静电力"),
    "1999_Shao_Numerical_Modelling_of_Saltation_in_the_Atmospheric_Surface_Layer":
        ("1999", "shao", "风沙跃移数值模拟"),
    "2000_Braithwaite_Sensitivity_of_mass_balance_of_five_Swiss_glaciers_to_temperature_changes_assessed_by_tuning_a_degree_day_model":
        ("2000", "braithwaite", "冰川物质平衡温度敏感性"),
    "2002_Dery_Large_scale_mass_balance_effects_of_blowing_snow_and_surface_sublimation":
        ("2002", "dery", "吹雪升华大尺度物质平衡"),
    "2003_Brown_Topographically_induced_waves_within_the_stable_boundary_layer":
        ("2003", "brown", "地形诱导稳定边界层波动"),
    "2004_Nemoto_Numerical_simulation_of_snow_saltation_and_suspension_in_a_turbulent_boundary_layer":
        ("2004", "nemoto", "雪跃移悬移数值模拟"),
    "2004_Strasser_Spatial_and_temporal_variability_of_meteorological_variables_at_Haut_Glacier_d_Arolla__Switzerland__during_the_ablation_season_2001_Measurements_and_simulations":
        ("2004", "strasser", "冰川气象变量时空变异性"),
    "2005_Nishimura_Blowing_snow_at_Mizuho_station__Antarctica":
        ("2005", "nishimura", "南极瑞穗站吹雪观测"),
    "2005_SHAO_A_scheme_for_drag_partition_over_rough_surfaces":
        ("2005", "shao", "粗糙地表阻力分配方案"),
    "2007_Blocken_CFD_simulation_of_the_atmospheric_boundary_layer_wall_function_problems":
        ("2007", "blocken", "大气边界层CFD壁面函数"),
    "2008_Ayotte_Computational_modelling_for_wind_energy_assessment":
        ("2008", "ayotte", "风能评估计算建模"),
    "2008_Lewis_The_Effect_of_Surface_Heating_on_Hill_Induced_Flow_Separation":
        ("2008", "lewis", "地表加热山丘流动分离"),
    "2008_Wang_Saltation_and_suspension_of_wind_blown_particle_movement":
        ("2008", "wang", "风沙跃移与悬移"),
    "2008_Zhang_Simulation_of_Snow_Drift_and_the_Effects_of_Snow__Particles_on_Wind":
        ("2008", "zhang", "雪漂移风场耦合模拟"),
    "2009_Gordon_Measurements_of_blowing_snow__Part_I_Particle_shape__size_distribution__velocity__and_number_flux_at_Churchill__Manitoba__Canada":
        ("2009", "gordon", "吹雪粒径速度通量测量"),
    "2009_Patton_Turbulent_Pressure_and_Velocity_Perturbations_Induced_by_Gentle_Hills_Covered_with_Sparse_and_Dense_Canopies":
        ("2009", "patton", "植被山丘湍流压力速度扰动"),
    "2011_Palm_Satellite_remote_sensing_of_blowing_snow_properties_over_Antarctica":
        ("2011", "palm", "南极吹雪卫星遥感"),
    "2011_Zwaaftink_Drifting_snow_sublimation_A_high_resolution_3_D_model_with_temperature_and_moisture_feedbacks":
        ("2011", "zwaaftink", "吹雪升华高分辨率三维模型"),
    "2012_Lu_Wind_tunnel_experiments_on_natural_snow_drift":
        ("2012", "lu", "天然雪漂移风洞实验"),
    "2013_Mott_Relative_importance_of_advective_heat_transport_and_boundary_layer_decoupling_in_the_melt_dynamics_of_a_patchy_snow_cover":
        ("2013", "mott", "斑状雪盖平流热与边界层解耦"),
    "2013_Warscher_Performance_of_complex_snow_cover_descriptions_in_a_distributed_hydrological_model_system_A_case_study_for_the_high_Alpine_terrain_of_the_Berchtesgaden_Alps":
        ("2013", "warscher", "分布式水文模型雪盖模拟"),
    "2014_Dai_Numerical_simulation_of_drifting_snow_sublimation_in_the_saltation_layer":
        ("2014", "dai", "跃移层吹雪升华数值模拟"),
    "2014_Nishimura_Snow_particle_speeds_in_drifting_snow":
        ("2014", "nishimura", "漂雪粒子速度"),
    "2014_Zhou_Wind_tunnel_test_of_snow_loads_on_a_stepped_flat_roof_using_different_granular_materials":
        ("2014", "zhou", "阶梯屋顶雪荷载风洞实验"),
    "2014_Zwaaftink_Modelling_Small_Scale_Drifting_Snow_with_a_Lagrangian_Stochastic_Model_Based_on_Large_Eddy_Simulations":
        ("2014", "zwaaftink", "小尺度漂雪拉格朗日随机模型"),
    "2015_Bleeg_Modeling_stable_thermal_stratification_and_its_impact_on_wind_flow_over_topography":
        ("2015", "bleeg", "稳定层结地形风场模拟"),
    "2015_Helbig_Fractional_snow_covered_area_parameterization_over_complex_topography":
        ("2015", "helbig", "复杂地形积雪面积参数化"),
    "2015_Randin_Validation_of_and_comparison_between_a_semidistributed_rainfall_runoff_hydrological_model__PREVAH__and_a_spatially_distributed_snow_evolution_model__SnowModel__for_snow_cover_prediction_in_mountain_ecosystems":
        ("2015", "randin", "山地积雪模型对比验证"),
    "2016_Huang_The_formation_of_snow_streamers_in_the_turbulent_atmosphere_boundary_layer":
        ("2016", "huang", "湍流边界层雪流线形成"),
    "2016_Huang_The_impacts_of_moisture_transport_on_drifting_snow_sublimation_in_the__saltation_layer":
        ("2016", "huang", "水汽输送对跃移层吹雪升华影响"),
    "2016_Liu_LES_study_of_turbulent_flow_fields_over_a_smooth_3_D_hill_and_a_smooth_2_D_ridge":
        ("2016", "liu", "三维山丘二维山脊湍流大涡模拟"),
    "2017_Comola_Fragmentation_of_wind_blown_snow_crystals":
        ("2017", "comola", "风吹雪晶体破碎"),
    "2017_Gerber_A_close_ridge_small_scale_atmospheric_flow_field_and_its_influence_on_snow_accumulation":
        ("2017", "gerber", "山脊小尺度流场对积雪影响"),
    "2017_Huang_The_significance_of_vertical_moisture_diffusion_on_drifting_snow_sublimation_near_snow_surface":
        ("2017", "huang", "近地面垂直水汽扩散对吹雪升华影响"),
    "2017_Li_Drifting_snow_and_its_sublimation_in_turbulent_boundary_layer":
        ("2017", "li", "湍流边界层漂雪升华"),
    "2017_SOMMER_Wind_tunnel_experiments_saltation_is_necessary_for_wind_packing":
        ("2017", "sommer", "跃移对风压实作用风洞实验"),
    "2017_Wang_Numerical_simulation_of_the_falling_snow_deposition_over_complex_terrain":
        ("2017", "wang", "复杂地形降雪沉积数值模拟"),
    "2017_Yang_Modelling_turbulent_boundary_layer_flow_over_fractal_like_multiscale_terrain_using_large_eddy_simulations_and_analytical_tools":
        ("2017", "yang", "分形多尺度地形湍流边界层模拟"),
    "2018_Li_A_Snow_Distribution_Model_Based_on_Snowfall_and_Snow_Drifting_Simulations_in_Mountain_Area":
        ("2018", "li", "山区降雪吹雪分布模型"),
    "2018_Schon_Merging_a_terrain_based_parameter_with_blowing_snow_fluxes_for_assessing_snow_redistribution_in_alpine_terrain":
        ("2018", "schon", "地形参数与吹雪通量融合"),
    "2018_Sharma_On_the_suitability_of_the_Thorpe_Mason_model_for_calculating_sublimation_of_saltating_snow":
        ("2018", "sharma", "跃移雪升华Thorpe-Mason模型"),
    "2018_Sommer_Investigation_of_a_wind_packing_event_in_Queen_Maud_Land__Antarctica":
        ("2018", "sommer", "南极风压实事件观测"),
    "2019_Amory_Brief_communication_Rare_ambient_saturation_during_drifting_snow_occurrences_at_a_coastal_location_of_East_Antarctica":
        ("2019", "amory", "南极海岸漂雪罕见饱和"),
    "2019_Comola_Preferential_Deposition_of_Snow_and_Dust_Over_Hills_Governing_Processes_and_Relevant_Scales":
        ("2019", "comola", "山丘雪尘优先沉积机制"),
    "2019_Dar_On_the_self_similarity_of_wind_turbine_wakes_in__a_complex_terrain_using_large_eddy_simulation":
        ("2019", "dar", "复杂地形风机尾流自相似性"),
    "2019_Gerber_The_Importance_of_Near_Surface_Winter_Precipitation_Processes_in_Complex_Alpine_Terrain":
        ("2019", "gerber", "高山近地面冬季降水过程"),
    "2019_Liu_Large_Eddy_Simulations_of_the_Flow_Over_an_Isolated_Three_Dimensional_Hill":
        ("2019", "liu", "孤立三维山丘大涡模拟"),
    "2019_Liu_Turbulent_Flow_Fields_Over_a_3D_Hill_Covered_by_Vegetation_Canopy_Through_Large_Eddy_Simulations":
        ("2019", "liu", "植被覆盖三维山丘湍流场"),
    "2019_Wang_The_Effect_of_Turbulence_on_Drifting_Snow_Sublimation":
        ("2019", "wang", "湍流对吹雪升华影响"),
    "2020_Finnigan_Boundary_Layer_Flow_Over_Complex_Topography":
        ("2020", "finnigan", "复杂地形边界层流动"),
    "2020_Walter_Radar_measurements_of_blowing_snow_off_a_mountain_ridge":
        ("2020", "walter", "山脊吹雪雷达测量"),
    "2021_Zheng_Modulation_of_turbulence_by_saltating_particles_on_erodible_bed_surface":
        ("2021", "zheng", "跃移颗粒湍流调制"),
    "2023_Sharma_Introducing_CRYOWRF_v1_0_multiscale_atmospheric_flow_simulations_with_advanced_snow_cover_modelling":
        ("2023", "sharma", "CRYOWRF多尺度雪气耦合模拟"),
    "2023_Wang_Drag_model_of_finite_sized_particle_in_turbulent_wall_bound_flow_over_sediment_bed":
        ("2023", "wang", "有限粒径颗粒湍流边界层阻力模型"),
}

def build_new_paper_id(old_dir, entry):
    """Build new paper_id from mapping entry"""
    year, author, zh_title = entry
    return f"{year}_{author}_{zh_title}"


def rename_paper(old_dir, new_paper_id):
    """重命名一个 paper 目录及其内部所有文件"""
    old_path = os.path.join(DATA_PAPERS, old_dir)
    new_path = os.path.join(DATA_PAPERS, new_paper_id)

    if not os.path.isdir(old_path):
        print(f"  SKIP: {old_dir} 不存在")
        return None

    if os.path.exists(new_path):
        print(f"  ERROR: 目标目录已存在 {new_paper_id}")
        return None

    print(f"  {old_dir}")
    print(f"  → {new_paper_id}")

    if DRY_RUN:
        return {'old_dir': old_dir, 'new_paper_id': new_paper_id, 'old_path': old_path, 'new_path': new_path}

    # Step 1: Rename all files inside the directory
    rename_plan = []
    for fname in os.listdir(old_path):
        old_fpath = os.path.join(old_path, fname)
        if not os.path.isfile(old_fpath):
            continue  # skip images/ dir and any other subdirs
        # Only rename files that start with old_dir prefix
        if fname.startswith(old_dir + '.'):
            suffix = fname[len(old_dir):]  # e.g. ".catalog.json"
            new_fname = new_paper_id + suffix
            new_fpath = os.path.join(old_path, new_fname)
            os.rename(old_fpath, new_fpath)
            rename_plan.append((old_fpath, new_fpath))
            print(f"    mv: {fname} → {new_fname}")

    # Step 2: Rename the directory itself
    os.rename(old_path, new_path)
    print(f"    mv dir: {old_dir} → {new_paper_id}")

    return {
        'old_dir': old_dir,
        'new_paper_id': new_paper_id,
        'old_path': old_path,
        'new_path': new_path,
        'rename_plan': rename_plan
    }


def update_json_content(old_dir, new_paper_id):
    """更新 catalog.json 和 metadata.json 中的 paper_id/source_id"""
    new_path = os.path.join(DATA_PAPERS, new_paper_id)

    # catalog.json
    cat_path = os.path.join(new_path, f'{new_paper_id}.catalog.json')
    if os.path.exists(cat_path):
        with open(cat_path, encoding='utf-8') as f:
            cat = json.load(f)
        changed = False
        if cat.get('paper_id') == old_dir:
            cat['paper_id'] = new_paper_id
            changed = True
        if cat.get('source_id') == old_dir:
            cat['source_id'] = new_paper_id
            changed = True
        if 'classification' in cat:
            tags_fields = ['topic_tags', 'methods_tags', 'phenomena_tags', 'material_tags']
            for field in tags_fields:
                if field in cat['classification']:
                    old_tags = cat['classification'][field]
                    if old_tags and all(not re.match(r'^[一-鿿]', t) for t in old_tags if t):
                        new_tags = translate_tags(old_tags)
                        if new_tags != old_tags:
                            cat['classification'][field] = new_tags
                            changed = True
        if changed:
            if APPLY:
                with open(cat_path, 'w', encoding='utf-8') as f:
                    json.dump(cat, f, ensure_ascii=False, indent=2)
                print(f"    updated: {new_paper_id}.catalog.json (paper_id/source_id/tags)")
            else:
                print(f"    would update: {new_paper_id}.catalog.json")

    # metadata.json
    meta_path = os.path.join(new_path, f'{new_paper_id}.metadata.json')
    if os.path.exists(meta_path):
        with open(meta_path, encoding='utf-8') as f:
            meta = json.load(f)
        changed = False
        if meta.get('source_id') == old_dir:
            meta['source_id'] = new_paper_id
            changed = True
        elif meta.get('source_id', '').isdigit():
            # Some meta files use numeric source_id, leave them
            pass
        if changed:
            if APPLY:
                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                print(f"    updated: {new_paper_id}.metadata.json")
            else:
                print(f"    would update: {new_paper_id}.metadata.json")


def update_ledger(old_dir, new_paper_id):
    """更新 paper_number_ledger.json"""
    if not os.path.exists(LEDGER_PATH):
        return

    with open(LEDGER_PATH, encoding='utf-8') as f:
        ledger = json.load(f)

    changed = False
    for pn, entry in ledger.get('items', {}).items():
        if entry.get('folder_name') == old_dir:
            entry['folder_name'] = new_paper_id
            entry['folder_path'] = f'data/papers/{new_paper_id}'
            changed = True
            print(f"    ledger: {old_dir} → {new_paper_id}")

    if changed and APPLY:
        with open(LEDGER_PATH, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)


# ── 主流程 ──────────────────────────────────────────
def main():
    print(f"{'='*60}")
    print(f"批量重命名英文论文目录")
    print(f"Mode: {'DRY RUN' if DRY_RUN else ('APPLY' if APPLY else 'PLAN ONLY')}")
    print(f"{'='*60}\n")

    all_old_dirs = list(PAPER_RENAME_MAP.keys())
    total = len(all_old_dirs)
    print(f"共 {total} 篇论文待重命名\n")

    results = []
    for i, old_dir in enumerate(all_old_dirs, 1):
        entry = PAPER_RENAME_MAP[old_dir]
        new_paper_id = build_new_paper_id(old_dir, entry)
        print(f"[{i}/{total}]", end="")
        result = rename_paper(old_dir, new_paper_id)
        if result:
            results.append(result)
            # Update content after rename (if apply mode)
            if APPLY:
                update_json_content(old_dir, new_paper_id)
                update_ledger(old_dir, new_paper_id)
        print()

    # Summary
    print(f"{'='*60}")
    print(f"完成。{'DRY RUN' if DRY_RUN else '已重命名'} {len(results)} 篇论文。")
    if DRY_RUN:
        print("运行 --apply 来执行实际重命名。")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
