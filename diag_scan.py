import time
from duplicate_finder import scanner

path = r'L:\MyStuff\Collection'
print('Scanning', path)

start = time.time()
files = scanner._collect_files([path])
print('collect_files', len(files), 'files', time.time() - start)

# count size groups
from collections import defaultdict
size_map = defaultdict(list)
for f in files:
    size_map[f.size].append(f)
size_groups = [g for g in size_map.values() if len(g) > 1]
print('size groups', len(size_groups), 'candidate files', sum(len(g) for g in size_groups))

start = time.time()
# partial hash only on candidate files
candidate_files = [e for g in size_groups for e in g]
for e in candidate_files:
    e.partial_hash = scanner._compute_partial_hash(e.path)
print('partial hash candidate files', len(candidate_files), time.time() - start)

start = time.time()
# full hash for candidate groups
partial_map = defaultdict(list)
for e in candidate_files:
    partial_map[(e.size, e.partial_hash)].append(e)
full_candidates = [e for g in partial_map.values() if len(g) > 1 for e in g]
print('full candidate files', len(full_candidates))
for e in full_candidates:
    e.full_hash = scanner._compute_full_hash_worker(e.path)
print('full hash candidate files', len(full_candidates), time.time() - start)

print('total elapsed', time.time() - start)
