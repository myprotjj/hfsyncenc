import os

search_folder = 'htiA0dXhVH4zZBb3N7sgp2x2cn2s0r1Tq8M'
print('Searching for source folder across drives...')
for drive in ['C:\\', 'D:\\', 'E:\\', 'F:\\']:
    if os.path.exists(drive):
        print(f'Checking {drive}...')
        try:
            for root, dirs, files in os.walk(drive):
                # skip windows, program files, appdata
                if any(x in root.lower() for x in ['windows', 'program files', 'appdata', '']):
                    continue
                for d in dirs:
                    if search_folder in d:
                        print('Found folder:', os.path.join(root, d))
        except Exception as e:
            print(f'Error walking {drive}:', e)
print('Search complete.')
