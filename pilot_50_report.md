# MeshOpt pilot report

50 meshes, 7 chains, 350 runs. Status counts: {'ok': 348, 'oom': 2}.

Cheap-feature extraction: median 0.097 s per mesh (must stay far below repair time).


## Summary

| profile                          | best chain     |   gap pts |   router saving % |   top5% time share |
|:---------------------------------|:---------------|----------:|------------------:|-------------------:|
| sdf (implicit-field models)      | udf256         |        18 |               5.5 |               16.4 |
| token (mesh LLMs, 500-10k faces) | dec+udf256+dec |        24 |               5.3 |               18.7 |
| cad (token + sharp edges)        | local+dec      |         2 |               0   |               40.2 |

Go/no-go rule of thumb: gap >= 10 points on some profile AND router saves >= 20% compute -> build MeshOpt. Thresholds are a starting point to agree with your advisor.


## Profile: sdf (implicit-field models)

50 meshes, 7 chains.

| chain                |   success % |   fail (timeout/oom/crash) % |   median time s |   p99 time s |   total time h |   median hausdorff |   median sharp recall |
|:---------------------|------------:|-----------------------------:|----------------:|-------------:|---------------:|-------------------:|----------------------:|
| udf256               |          76 |                            4 |           3.625 |       13.259 |          0.062 |              0.01  |                 0.001 |
| udf256+dec           |          74 |                            0 |           8.537 |       39.35  |          0.157 |              0.011 |                 0.038 |
| dec+udf256+dec       |          74 |                            0 |           8.742 |       35.597 |          0.156 |              0.011 |                 0.038 |
| udf128               |          52 |                            0 |           0.42  |        0.79  |          0.006 |              0.019 |                 0     |
| manifoldplus+dec+fix |          46 |                            0 |          12.911 |       63.017 |          0.244 |              0.006 |                 0.481 |
| local+dec            |          28 |                            0 |           0.147 |        5.518 |          0.009 |              0.003 |                 1     |
| local                |          28 |                            0 |           0.128 |        1.59  |          0.004 |              0.003 |                 1     |

**Q1 Silver bullet.** Best single chain: `udf256` (76.0% success). Per-mesh oracle: 94.0%. Gap: **18.0 points** (9 meshes only another chain can fix). `udf256` is the cheapest successful chain for 21% of meshes.

Filter-and-discard baseline (`local` then drop failures) keeps 28.0% of meshes.

**Q2 Heavy tails.** For `udf256`: slowest 1% of meshes take 7% of its total time, slowest 5% take 16%. p99/median = 4x.

**Q3 Predictable from cheap features?** Cross-validated AUC for "this chain succeeds": `dec+udf256+dec` 0.48, `local` 0.80, `local+dec` 0.80, `manifoldplus+dec+fix` 0.74, `udf128` 0.56, `udf256` 0.60, `udf256+dec` 0.48. (0.5 = random, >0.75 = useful.)

**Q4 Policies.**

| policy             |   kept % |   total compute h |   sec per kept mesh |
|:-------------------|---------:|------------------:|--------------------:|
| best single chain  |       76 |             0.062 |               5.862 |
| try-except cascade |       94 |             0.071 |               5.442 |
| learned router     |       94 |             0.067 |               5.143 |
| oracle             |       94 |             0.023 |               1.76  |

Learned router vs try-except: +0.0 points kept, 5% less compute.

**Verdict for this profile:** PARTIAL: different meshes need different chains, but cheap features don't route well yet; focus on adaptive execution.


## Profile: token (mesh LLMs, 500-10k faces)

50 meshes, 7 chains.

| chain                |   success % |   fail (timeout/oom/crash) % |   median time s |   p99 time s |   total time h |   median hausdorff |   median sharp recall |
|:---------------------|------------:|-----------------------------:|----------------:|-------------:|---------------:|-------------------:|----------------------:|
| dec+udf256+dec       |          42 |                            0 |           8.742 |       35.597 |          0.156 |              0.011 |                 0.038 |
| udf256+dec           |          42 |                            0 |           8.537 |       39.35  |          0.157 |              0.011 |                 0.038 |
| manifoldplus+dec+fix |          40 |                            0 |          12.911 |       63.017 |          0.244 |              0.006 |                 0.481 |
| local+dec            |          24 |                            0 |           0.147 |        5.518 |          0.009 |              0.003 |                 1     |
| local                |          14 |                            0 |           0.128 |        1.59  |          0.004 |              0.003 |                 1     |
| udf128               |           0 |                            0 |           0.42  |        0.79  |          0.006 |              0.019 |                 0     |
| udf256               |           0 |                            4 |           3.625 |       13.259 |          0.062 |              0.01  |                 0.001 |

**Q1 Silver bullet.** Best single chain: `dec+udf256+dec` (42.0% success). Per-mesh oracle: 66.0%. Gap: **24.0 points** (12 meshes only another chain can fix). `dec+udf256+dec` is the cheapest successful chain for 21% of meshes.

Filter-and-discard baseline (`local` then drop failures) keeps 14.0% of meshes.

**Q2 Heavy tails.** For `dec+udf256+dec`: slowest 1% of meshes take 6% of its total time, slowest 5% take 19%. p99/median = 4x.

**Q3 Predictable from cheap features?** Cross-validated AUC for "this chain succeeds": `dec+udf256+dec` 0.73, `local` 0.93, `local+dec` 0.79, `manifoldplus+dec+fix` 0.75, `udf256+dec` 0.73. (0.5 = random, >0.75 = useful.)

**Q4 Policies.**

| policy             |   kept % |   total compute h |   sec per kept mesh |
|:-------------------|---------:|------------------:|--------------------:|
| best single chain  |       42 |             0.156 |              26.728 |
| try-except cascade |       66 |             0.376 |              41.015 |
| learned router     |       66 |             0.356 |              38.859 |
| oracle             |       66 |             0.068 |               7.426 |

Learned router vs try-except: +0.0 points kept, 5% less compute.

**Verdict for this profile:** PARTIAL: different meshes need different chains, but cheap features don't route well yet; focus on adaptive execution.


## Profile: cad (token + sharp edges)

50 meshes, 7 chains.

| chain                |   success % |   fail (timeout/oom/crash) % |   median time s |   p99 time s |   total time h |   median hausdorff |   median sharp recall |
|:---------------------|------------:|-----------------------------:|----------------:|-------------:|---------------:|-------------------:|----------------------:|
| local+dec            |          20 |                            0 |           0.147 |        5.518 |          0.009 |              0.003 |                 1     |
| local                |          14 |                            0 |           0.128 |        1.59  |          0.004 |              0.003 |                 1     |
| manifoldplus+dec+fix |           2 |                            0 |          12.911 |       63.017 |          0.244 |              0.006 |                 0.481 |
| dec+udf256+dec       |           0 |                            0 |           8.742 |       35.597 |          0.156 |              0.011 |                 0.038 |
| udf128               |           0 |                            0 |           0.42  |        0.79  |          0.006 |              0.019 |                 0     |
| udf256               |           0 |                            4 |           3.625 |       13.259 |          0.062 |              0.01  |                 0.001 |
| udf256+dec           |           0 |                            0 |           8.537 |       39.35  |          0.157 |              0.011 |                 0.038 |

**Q1 Silver bullet.** Best single chain: `local+dec` (20.0% success). Per-mesh oracle: 22.0%. Gap: **2.0 points** (1 meshes only another chain can fix). `local+dec` is the cheapest successful chain for 64% of meshes.

Filter-and-discard baseline (`local` then drop failures) keeps 14.0% of meshes.

**Q2 Heavy tails.** For `local+dec`: slowest 1% of meshes take 19% of its total time, slowest 5% take 40%. p99/median = 38x.

**Q3 Predictable from cheap features?** Cross-validated AUC for "this chain succeeds": `local` 0.93, `local+dec` 0.76. (0.5 = random, >0.75 = useful.)

**Q4 Policies.**

| policy             |   kept % |   total compute h |   sec per kept mesh |
|:-------------------|---------:|------------------:|--------------------:|
| best single chain  |       20 |             0.009 |               3.386 |
| try-except cascade |       22 |             0.538 |             175.993 |
| learned router     |       22 |             0.538 |             175.95  |
| oracle             |       22 |             0.003 |               1.107 |

Learned router vs try-except: +0.0 points kept, 0% less compute.

**Verdict for this profile:** NARROW: one chain mostly wins, but heavy tails make adaptive execution worth a smaller paper.
