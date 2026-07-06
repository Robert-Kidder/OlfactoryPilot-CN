import re
import os
import datetime

# Configuration
EPICS_FILE = 'docs/epics.md'
OUTPUT_FILE = 'docs/sprint-artifacts/sprint-status.yaml'
PROJECT_NAME = 'OlfactoryPilot'
PROJECT_KEY = 'OLFACTORY'
TRACKING_SYSTEM = 'file-system'
STORY_LOCATION = 'docs/sprint-artifacts'

def to_kebab_case(s):
    s = re.sub(r'[^a-zA-Z0-9\s-]', '', s).strip()
    s = re.sub(r'\s+', '-', s)
    return s.lower()

def generate_status():
    if not os.path.exists(EPICS_FILE):
        print(f"Error: {EPICS_FILE} not found.")
        return

    with open(EPICS_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    development_status = {}
    current_epic = None

    # Regex patterns
    epic_pattern = re.compile(r'###? Epic (\d+): (.+)')
    # Handle optional text like (Refined) before the colon
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
        elif story_match: # Story can exist without current_epic context if we track it globally, but usually it's nested
             # If story pattern matches, we try to extract ID
            story_id_raw = story_match.group(1)
            story_title = story_match.group(2).strip()
            
            # Ensure we have an epic context or try to infer from ID (e.g. Story 1.5 -> Epic 1)
            if not current_epic:
                 major_id = story_id_raw.split('.')[0]
                 current_epic = f'epic-{major_id}'
                 if current_epic not in development_status:
                     development_status[current_epic] = 'backlog'

            story_id_kebab = story_id_raw.replace('.', '-')
            story_key = f'{story_id_kebab}-{to_kebab_case(story_title)}'
            development_status[story_key] = 'backlog'

    # Add retrospective entries
    epic_keys = [k for k in development_status.keys() if k.startswith('epic-') and not k[5].isdigit() == False] # Simple check for epic-X
    # Better check: keys that match epic-\d+
    
    unique_epics = set()
    for key in development_status.keys():
        if re.match(r'^epic-\d+$', key):
            unique_epics.add(key)
            
    for epic_key in unique_epics:
        retro_key = f'{epic_key}-retrospective'
        if retro_key not in development_status:
            development_status[retro_key] = 'optional'

    # Preserve existing statuses if file exists
    if os.path.exists(OUTPUT_FILE):
        print(f"Reading existing status from {OUTPUT_FILE}...")
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            existing_lines = f.readlines()
        
        current_key = None
        for line in existing_lines:
            line = line.strip()
            if ':' in line and not line.startswith('#'):
                key, val = line.split(':', 1)
                key = key.strip()
                val = val.strip()
                if key in development_status:
                    # Only preserve if not backlog (or maybe preserve everything? Let's preserve everything to be safe)
                    development_status[key] = val

    # Generate Output
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    
    output = []
    output.append(f"# Sprint Status File")
    output.append(f"# Generated: {timestamp}")
    output.append(f"generated: {timestamp}")
    output.append(f"project: {PROJECT_NAME}")
    output.append(f"project_key: {PROJECT_KEY}")
    output.append(f"tracking_system: {TRACKING_SYSTEM}")
    output.append(f"story_location: {STORY_LOCATION}")
    output.append("")
    output.append("development_status:")

    # Sort keys: Epics first, then their stories, then retro
    # Extract unique epic numbers
    epic_nums = sorted([int(k.split('-')[1]) for k in unique_epics])
    
    for num in epic_nums:
        epic_key = f'epic-{num}'
        output.append(f"  {epic_key}: {development_status.get(epic_key, 'backlog')}")
        
        # Stories for this epic
        stories = []
        for key in development_status.keys():
            if key.startswith(f'{num}-'):
                stories.append(key)
        
        # Sort stories by secondary ID (e.g. 1-1, 1-2, 1-10)
        # Helper to sort 1-1, 1-2, 1-10 correctly
        def story_sort_key(s):
            parts = s.split('-')
            # parts[0] is epic num, parts[1] is story num
            try:
                return int(parts[1])
            except:
                return 0
        
        stories.sort(key=story_sort_key)
        
        for story in stories:
            output.append(f"  {story}: {development_status.get(story, 'backlog')}")
            
        # Retrospective
        retro_key = f'epic-{num}-retrospective'
        output.append(f"  {retro_key}: {development_status.get(retro_key, 'optional')}")
        output.append("") # Spacer

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))
    
    print(f"Successfully generated {OUTPUT_FILE}")

if __name__ == "__main__":
    generate_status()
