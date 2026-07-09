import datetime
import os
import re

# 配置
EPICS_FILE = 'docs/epics.md'
OUTPUT_FILE = 'docs/sprint-artifacts/sprint-status.yaml'
PROJECT_NAME = 'OlfactoryPilot-CN'
PROJECT_KEY = 'OLFACTORY'
TRACKING_SYSTEM = 'file-system'
STORY_LOCATION = 'docs/sprint-artifacts'
STORY_SLUG_OVERRIDES = {
    '1.0': 'project-scaffold-and-ci-baseline',
    '1.1': 'device-self-check-and-status-report',
    '1.2': 'safe-start-airflow-interlock',
    '1.3': 'global-safety-toolbar',
    '1.4': 'safe-shutdown-and-valve-reset',
    '1.5': 'hardware-simulation-layer-mock-hal',
    '2.1': 'real-time-breath-visualization',
    '2.2': 'threshold-tuning-and-feedback',
    '2.3': 'valve-matrix-manual-control',
    '2.4': 'flow-rate-controls',
    '2.5': 'variant-aware-pre-test-ui',
    '2.6': 'automatic-breath-calibration-session',
    '2.7': 'calibration-ui-optimization',
    '3.1': 'protocol-file-parsing-txtcsv',
    '3.2': 'breath-gated-stimulation',
    '3.3': 'manual-vs-ttl-trigger-modes',
    '3.4': 'low-jitter-actuation-20ms',
    '3.5': 'session-file-naming-and-logging',
    '4.1': 'cleaning-automation',
    '4.2': 'configurable-com-and-ni-ids',
    '4.3': 'chinese-ui-localization',
    '4.4': 'compensation-logic-automation',
}

def to_kebab_case(s):
    s = re.sub(r'[^a-zA-Z0-9\s-]', '', s).strip()
    s = re.sub(r'\s+', '-', s)
    return s.lower()

def generate_status():
    if not os.path.exists(EPICS_FILE):
        print(f"错误: 未找到 {EPICS_FILE}。")
        return

    with open(EPICS_FILE, encoding='utf-8') as f:
        content = f.read()

    development_status = {}
    current_epic = None

    # 正则模式
    epic_pattern = re.compile(r'###? Epic (\d+): (.+)')
    # 兼容 Story 编号和标题之间的可选文本。
    story_pattern = re.compile(r'####? Story (\d+\.\d+)(?: [^:]*)?: (.+)')

    lines = content.splitlines()
    for line in lines:
        epic_match = epic_pattern.match(line)
        story_match = story_pattern.match(line)

        if epic_match:
            epic_num = epic_match.group(1)
            # title = epic_match.group(2).strip()
            current_epic = f'epic-{epic_num}'
            development_status[current_epic] = 'backlog'
        elif story_match:
            story_id_raw = story_match.group(1)
            story_title = story_match.group(2).strip()

            # 如果缺少当前 Epic 上下文，则从 Story 编号推断。
            if not current_epic:
                 major_id = story_id_raw.split('.')[0]
                 current_epic = f'epic-{major_id}'
                 if current_epic not in development_status:
                     development_status[current_epic] = 'backlog'

            story_id_kebab = story_id_raw.replace('.', '-')
            story_slug = STORY_SLUG_OVERRIDES.get(story_id_raw) or to_kebab_case(story_title) or 'story'
            story_key = f'{story_id_kebab}-{story_slug}'
            development_status[story_key] = 'backlog'

    # 添加回顾条目。
    unique_epics = set()
    for key in development_status.keys():
        if re.match(r'^epic-\d+$', key):
            unique_epics.add(key)

    for epic_key in unique_epics:
        retro_key = f'{epic_key}-retrospective'
        if retro_key not in development_status:
            development_status[retro_key] = 'optional'

    # 如果状态文件已存在，保留已经记录的状态值。
    if os.path.exists(OUTPUT_FILE):
        print(f"正在读取已有状态: {OUTPUT_FILE}...")
        with open(OUTPUT_FILE, encoding='utf-8') as f:
            existing_lines = f.readlines()

        for line in existing_lines:
            line = line.strip()
            if ':' in line and not line.startswith('#'):
                key, val = line.split(':', 1)
                key = key.strip()
                val = val.strip()
                if key in development_status:
                    development_status[key] = val

    # 生成输出。
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    output = []
    output.append("# Sprint 状态文件")
    output.append(f"# 生成时间: {timestamp}")
    output.append(f"generated: {timestamp}")
    output.append(f"project: {PROJECT_NAME}")
    output.append(f"project_key: {PROJECT_KEY}")
    output.append(f"tracking_system: {TRACKING_SYSTEM}")
    output.append(f"story_location: {STORY_LOCATION}")
    output.append("")
    output.append("development_status:")

    # 排序：先 Epic，再 Story，最后回顾。
    epic_nums = sorted([int(k.split('-')[1]) for k in unique_epics])

    for num in epic_nums:
        epic_key = f'epic-{num}'
        output.append(f"  {epic_key}: {development_status.get(epic_key, 'backlog')}")

        stories = []
        for key in development_status.keys():
            if key.startswith(f'{num}-'):
                stories.append(key)

        # 按 Story 小编号排序，例如 1-1、1-2、1-10。
        def story_sort_key(s):
            parts = s.split('-')
            try:
                return int(parts[1])
            except ValueError:
                return 0

        stories.sort(key=story_sort_key)

        for story in stories:
            output.append(f"  {story}: {development_status.get(story, 'backlog')}")

        retro_key = f'epic-{num}-retrospective'
        output.append(f"  {retro_key}: {development_status.get(retro_key, 'optional')}")
        output.append("") # Spacer

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))

    print(f"已生成 {OUTPUT_FILE}")

if __name__ == "__main__":
    generate_status()
