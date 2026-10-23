# MeshOpt pilot report

1987 meshes, 7 chains, 13909 runs. Status counts: {'ok': 13591, 'oom': 298, 'timeout': 15, 'error': 5}.

Cheap-feature extraction: median 0.088 s per mesh (must stay far below repair time).


## Summary

| profile                          | best chain           |   gap pts |   router saving % |   top5% time share |
|:---------------------------------|:---------------------|----------:|------------------:|-------------------:|
| sdf (implicit-field models)      | udf256               |      13.4 |               9.5 |               11.7 |
| token (mesh LLMs, 500-10k faces) | manifoldplus+dec+fix |      21.5 |               7.6 |               24.6 |
| cad (token + sharp edges)        | local+dec            |       3.7 |               1.1 |               40.7 |

Go/no-go rule of thumb: gap >= 10 points on some profile AND router saves >= 20% compute -> build MeshOpt. Thresholds are a starting point to agree with your advisor.


## Profile: sdf (implicit-field models)

1987 meshes, 7 chains.

| chain                |   success % |   fail (timeout/oom/crash) % |   median time s |   p99 time s |   total time h |   median hausdorff |   median sharp recall |
|:---------------------|------------:|-----------------------------:|----------------:|-------------:|---------------:|-------------------:|----------------------:|
| udf256               |      70.609 |                        2.869 |           3.194 |        9.79  |          1.917 |              0.011 |                 0     |
| udf256+dec           |      69.602 |                        2.114 |           6.684 |       28.692 |          4.347 |              0.012 |                 0.021 |
| dec+udf256+dec       |      69.502 |                        2.013 |           7.222 |       29.554 |          4.812 |              0.012 |                 0.022 |
| manifoldplus+dec+fix |      50.73  |                        2.768 |           9.953 |      110.132 |         10.547 |              0.006 |                 0.472 |
| udf128               |      41.117 |                        2.416 |           0.382 |        6.028 |          0.302 |              0.021 |                 0     |
| local                |      31.857 |                        1.862 |           0.14  |        6.497 |          0.277 |              0     |                 1     |
| local+dec            |      31.505 |                        1.963 |           0.249 |       18.86  |          1.09  |              0.002 |                 0.997 |

**Q1 Silver bullet.** Best single chain: `udf256` (70.6% success). Per-mesh oracle: 84.0%. Gap: **13.4 points** (267 meshes only another chain can fix). `udf256` is the cheapest successful chain for 21% of meshes.

Filter-and-discard baseline (`local` then drop failures) keeps 31.9% of meshes.

**Q2 Heavy tails.** For `udf256`: slowest 1% of meshes take 4% of its total time, slowest 5% take 12%. p99/median = 3x.

**Q3 Predictable from cheap features?** Cross-validated AUC for "this chain succeeds": `dec+udf256+dec` 0.76, `local` 0.86, `local+dec` 0.87, `manifoldplus+dec+fix` 0.74, `udf128` 0.78, `udf256` 0.76, `udf256+dec` 0.76. (0.5 = random, >0.75 = useful.)

**Q4 Policies.**

| policy             |   kept % |   total compute h |   sec per kept mesh |
|:-------------------|---------:|------------------:|--------------------:|
| best single chain  |   70.609 |             1.917 |               4.919 |
| try-except cascade |   84.046 |             5.263 |              11.345 |
| learned router     |   84.046 |             4.762 |              10.266 |
| oracle             |   84.046 |             0.727 |               1.566 |

Learned router vs try-except: +0.0 points kept, 10% less compute.

**Verdict for this profile:** PARTIAL: different meshes need different chains, but cheap features don't route well yet; focus on adaptive execution.


## Profile: token (mesh LLMs, 500-10k faces)

1987 meshes, 7 chains.

| chain                |   success % |   fail (timeout/oom/crash) % |   median time s |   p99 time s |   total time h |   median hausdorff |   median sharp recall |
|:---------------------|------------:|-----------------------------:|----------------:|-------------:|---------------:|-------------------:|----------------------:|
| manifoldplus+dec+fix |      42.375 |                        2.768 |           9.953 |      110.132 |         10.547 |              0.006 |                 0.472 |
| udf256+dec           |      34.323 |                        2.114 |           6.684 |       28.692 |          4.347 |              0.012 |                 0.021 |
| dec+udf256+dec       |      34.122 |                        2.013 |           7.222 |       29.554 |          4.812 |              0.012 |                 0.022 |
| local+dec            |      24.811 |                        1.963 |           0.249 |       18.86  |          1.09  |              0.002 |                 0.997 |
| local                |      12.028 |                        1.862 |           0.14  |        6.497 |          0.277 |              0     |                 1     |
| udf256               |       0.05  |                        2.869 |           3.194 |        9.79  |          1.917 |              0.011 |                 0     |
| udf128               |       0     |                        2.416 |           0.382 |        6.028 |          0.302 |              0.021 |                 0     |

**Q1 Silver bullet.** Best single chain: `manifoldplus+dec+fix` (42.4% success). Per-mesh oracle: 63.9%. Gap: **21.5 points** (427 meshes only another chain can fix). `manifoldplus+dec+fix` is the cheapest successful chain for 32% of meshes.

Filter-and-discard baseline (`local` then drop failures) keeps 12.0% of meshes.

**Q2 Heavy tails.** For `manifoldplus+dec+fix`: slowest 1% of meshes take 6% of its total time, slowest 5% take 25%. p99/median = 11x.

**Q3 Predictable from cheap features?** Cross-validated AUC for "this chain succeeds": `dec+udf256+dec` 0.80, `local` 0.95, `local+dec` 0.87, `manifoldplus+dec+fix` 0.76, `udf256+dec` 0.80. (0.5 = random, >0.75 = useful.)

**Q4 Policies.**

| policy             |   kept % |   total compute h |   sec per kept mesh |
|:-------------------|---------:|------------------:|--------------------:|
| best single chain  |   42.375 |            10.547 |              45.093 |
| try-except cascade |   63.865 |            13.017 |              36.927 |
| learned router     |   63.865 |            12.022 |              34.106 |
| oracle             |   63.865 |             2.394 |               6.791 |

Learned router vs try-except: +0.0 points kept, 8% less compute.

**Verdict for this profile:** PARTIAL: different meshes need different chains, but cheap features don't route well yet; focus on adaptive execution.


## Profile: cad (token + sharp edges)

1987 meshes, 7 chains.

| chain                |   success % |   fail (timeout/oom/crash) % |   median time s |   p99 time s |   total time h |   median hausdorff |   median sharp recall |
|:---------------------|------------:|-----------------------------:|----------------:|-------------:|---------------:|-------------------:|----------------------:|
| local+dec            |      17.011 |                        1.963 |           0.249 |       18.86  |          1.09  |              0.002 |                 0.997 |
| local                |      11.827 |                        1.862 |           0.14  |        6.497 |          0.277 |              0     |                 1     |
| manifoldplus+dec+fix |       5.033 |                        2.768 |           9.953 |      110.132 |         10.547 |              0.006 |                 0.472 |
| dec+udf256+dec       |       0.201 |                        2.013 |           7.222 |       29.554 |          4.812 |              0.012 |                 0.022 |
| udf256+dec           |       0.201 |                        2.114 |           6.684 |       28.692 |          4.347 |              0.012 |                 0.021 |
| udf128               |       0     |                        2.416 |           0.382 |        6.028 |          0.302 |              0.021 |                 0     |
| udf256               |       0     |                        2.869 |           3.194 |        9.79  |          1.917 |              0.011 |                 0     |

**Q1 Silver bullet.** Best single chain: `local+dec` (17.0% success). Per-mesh oracle: 20.7%. Gap: **3.7 points** (73 meshes only another chain can fix). `local+dec` is the cheapest successful chain for 56% of meshes.

Filter-and-discard baseline (`local` then drop failures) keeps 11.8% of meshes.

**Q2 Heavy tails.** For `local+dec`: slowest 1% of meshes take 12% of its total time, slowest 5% take 41%. p99/median = 76x.

**Q3 Predictable from cheap features?** Cross-validated AUC for "this chain succeeds": `local` 0.95, `local+dec` 0.90, `manifoldplus+dec+fix` 0.91. (0.5 = random, >0.75 = useful.)

**Q4 Policies.**

| policy             |   kept % |   total compute h |   sec per kept mesh |
|:-------------------|---------:|------------------:|--------------------:|
| best single chain  |   17.011 |             1.09  |              11.612 |
| try-except cascade |   20.684 |            20.95  |             183.504 |
| learned router     |   20.684 |            20.726 |             181.537 |
| oracle             |   20.684 |             0.275 |               2.409 |

Learned router vs try-except: +0.0 points kept, 1% less compute.

**Verdict for this profile:** NARROW: one chain mostly wins, but heavy tails make adaptive execution worth a smaller paper.
