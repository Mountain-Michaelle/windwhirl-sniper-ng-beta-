import sys

filepath = sys.argv[1]
start = int(sys.argv[2])
end = int(sys.argv[3])

with open(filepath, 'r') as f:
    lines = f.readlines()
for i in range(start - 1, min(end, len(lines))):
    print(f'{i+1:4d}:{lines[i]}', end='')
