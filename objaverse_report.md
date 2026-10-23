# MeshOpt pilot report

993 meshes, 16 chains, 15888 runs. Status counts: {'ok': 15089, 'timeout': 531, 'oom': 193, 'error': 75}.

Cheap-feature extraction: median 0.079 s per mesh (must stay far below repair time).


## Summary

| profile                          | best chain           |   gap pts |   router saving % |   top5% time share |
|:---------------------------------|:---------------------|----------:|------------------:|-------------------:|
| sdf (implicit-field models)      | udf256               |      17.1 |              27.6 |               10.1 |
| token (mesh LLMs, 500-10k faces) | manifoldplus+dec+fix |      26.2 |              11.5 |               23.2 |
| cad (token + sharp edges)        | ftetwild+dec+fix     |      10.8 |               2.9 |               12.8 |

Go/no-go rule of thumb: gap >= 10 points on some profile AND router saves >= 20% compute -> build MeshOpt. Thresholds are a starting point to agree with your advisor.


## Profile: sdf (implicit-field models)

993 meshes, 16 chains.

| chain                |   success % |   fail (timeout/oom/crash) % |   median time s |   p99 time s |   total time h |   median hausdorff |   median sharp recall |
|:---------------------|------------:|-----------------------------:|----------------:|-------------:|---------------:|-------------------:|----------------------:|
| udf256               |      72.91  |                        1.511 |           2.641 |        7.1   |          0.781 |              0.011 |                 0     |
| udf256+dec           |      71.299 |                        1.208 |           4.853 |       20.522 |          1.563 |              0.012 |                 0.021 |
| dec+udf256+dec       |      70.997 |                        1.108 |           5.245 |       21.637 |          1.715 |              0.012 |                 0.022 |
| udf256+remesh        |      58.409 |                        2.518 |           6.265 |      120.039 |          4.525 |              0.016 |                 0.069 |
| manifoldplus+dec+fix |      51.057 |                        1.007 |           6.642 |       61.293 |          3.34  |              0.006 |                 0.467 |
| udf128               |      42.095 |                        1.208 |           0.281 |        2.898 |          0.1   |              0.021 |                 0     |
| wn256                |      41.793 |                       13.998 |          13.888 |      120.101 |          9.424 |              0.02  |                 0.599 |
| wn256+dec            |      40.685 |                       13.494 |          16.705 |      120.101 |          9.902 |              0.024 |                 0.335 |
| ftetwild+dec+fix     |      35.448 |                       20.443 |          35.527 |      120.101 |         13.059 |              0.025 |                 0.798 |
| local                |      32.729 |                        1.007 |           0.08  |        3.883 |          0.105 |              0     |                 1     |
| local+dec            |      32.528 |                        1.208 |           0.145 |       12.931 |          0.378 |              0.002 |                 0.998 |
| local+remesh         |      30.715 |                        5.136 |           1.207 |      120.1   |          3.535 |              0.005 |                 0.995 |
| cull+local           |      27.19  |                        3.726 |           0.58  |      120.1   |          2.224 |              0.003 |                 1     |
| cull+local+dec       |      26.888 |                        3.827 |           0.979 |      120.1   |          2.472 |              0.005 |                 0.946 |
| meshfix              |      15.911 |                        4.632 |           0.353 |      120.1   |          2.123 |              0.035 |                 0.96  |
| meshfix+dec          |      14.199 |                        4.431 |           0.528 |      120.098 |          2.497 |              0.039 |                 0.855 |

**Q1 Silver bullet.** Best single chain: `udf256` (72.9% success). Per-mesh oracle: 90.0%. Gap: **17.1 points** (170 meshes only another chain can fix). `udf256` is the cheapest successful chain for 19% of meshes.

Filter-and-discard baseline (`local` then drop failures) keeps 32.7% of meshes.

**Q2 Heavy tails.** For `udf256`: slowest 1% of meshes take 3% of its total time, slowest 5% take 10%. p99/median = 3x.

**Q3 Predictable from cheap features?** Cross-validated AUC for "this chain succeeds": `cull+local` 0.86, `cull+local+dec` 0.87, `dec+udf256+dec` 0.73, `ftetwild+dec+fix` 0.77, `local` 0.87, `local+dec` 0.86, `local+remesh` 0.87, `manifoldplus+dec+fix` 0.75, `meshfix` 0.88, `meshfix+dec` 0.91, `udf128` 0.74, `udf256` 0.77, `udf256+dec` 0.75, `udf256+remesh` 0.70, `wn256` 0.68, `wn256+dec` 0.69. (0.5 = random, >0.75 = useful.)

**Q4 Policies.**

| policy             |   kept % |   total compute h |   sec per kept mesh |
|:-------------------|---------:|------------------:|--------------------:|
| best single chain  |    72.91 |             0.781 |               3.883 |
| try-except cascade |    90.03 |            13.749 |              55.366 |
| learned router     |    90.03 |             9.958 |              40.098 |
| oracle             |    90.03 |             0.568 |               2.287 |

Learned router vs try-except: +0.0 points kept, 28% less compute.

**Verdict for this profile:** GO: real heterogeneity and a router that pays off.


## Profile: token (mesh LLMs, 500-10k faces)

993 meshes, 16 chains.

| chain                |   success % |   fail (timeout/oom/crash) % |   median time s |   p99 time s |   total time h |   median hausdorff |   median sharp recall |
|:---------------------|------------:|-----------------------------:|----------------:|-------------:|---------------:|-------------------:|----------------------:|
| manifoldplus+dec+fix |      43.907 |                        1.007 |           6.642 |       61.293 |          3.34  |              0.006 |                 0.467 |
| udf256+dec           |      36.052 |                        1.208 |           4.853 |       20.522 |          1.563 |              0.012 |                 0.021 |
| dec+udf256+dec       |      35.75  |                        1.108 |           5.245 |       21.637 |          1.715 |              0.012 |                 0.022 |
| ftetwild+dec+fix     |      29.104 |                       20.443 |          35.527 |      120.101 |         13.059 |              0.025 |                 0.798 |
| wn256+dec            |      28.6   |                       13.494 |          16.705 |      120.101 |          9.902 |              0.024 |                 0.335 |
| local+dec            |      25.881 |                        1.208 |           0.145 |       12.931 |          0.378 |              0.002 |                 0.998 |
| local+remesh         |      21.047 |                        5.136 |           1.207 |      120.1   |          3.535 |              0.005 |                 0.995 |
| cull+local+dec       |      19.94  |                        3.827 |           0.979 |      120.1   |          2.472 |              0.005 |                 0.946 |
| udf256+remesh        |      15.408 |                        2.518 |           6.265 |      120.039 |          4.525 |              0.016 |                 0.069 |
| local                |      11.581 |                        1.007 |           0.08  |        3.883 |          0.105 |              0     |                 1     |
| cull+local           |       9.366 |                        3.726 |           0.58  |      120.1   |          2.224 |              0.003 |                 1     |
| meshfix+dec          |       8.258 |                        4.431 |           0.528 |      120.098 |          2.497 |              0.039 |                 0.855 |
| meshfix              |       3.927 |                        4.632 |           0.353 |      120.1   |          2.123 |              0.035 |                 0.96  |
| wn256                |       0.201 |                       13.998 |          13.888 |      120.101 |          9.424 |              0.02  |                 0.599 |
| udf256               |       0     |                        1.511 |           2.641 |        7.1   |          0.781 |              0.011 |                 0     |
| udf128               |       0     |                        1.208 |           0.281 |        2.898 |          0.1   |              0.021 |                 0     |

**Q1 Silver bullet.** Best single chain: `manifoldplus+dec+fix` (43.9% success). Per-mesh oracle: 70.1%. Gap: **26.2 points** (260 meshes only another chain can fix). `manifoldplus+dec+fix` is the cheapest successful chain for 23% of meshes.

Filter-and-discard baseline (`local` then drop failures) keeps 11.6% of meshes.

**Q2 Heavy tails.** For `manifoldplus+dec+fix`: slowest 1% of meshes take 6% of its total time, slowest 5% take 23%. p99/median = 9x.

**Q3 Predictable from cheap features?** Cross-validated AUC for "this chain succeeds": `cull+local` 0.94, `cull+local+dec` 0.86, `dec+udf256+dec` 0.78, `ftetwild+dec+fix` 0.81, `local` 0.94, `local+dec` 0.87, `local+remesh` 0.89, `manifoldplus+dec+fix` 0.75, `meshfix` 0.92, `meshfix+dec` 0.93, `udf256+dec` 0.77, `udf256+remesh` 0.75, `wn256+dec` 0.76. (0.5 = random, >0.75 = useful.)

**Q4 Policies.**

| policy             |   kept % |   total compute h |   sec per kept mesh |
|:-------------------|---------:|------------------:|--------------------:|
| best single chain  |   43.907 |             3.34  |              27.58  |
| try-except cascade |   70.091 |            32.451 |             167.853 |
| learned router     |   70.091 |            28.73  |             148.602 |
| oracle             |   70.091 |             0.931 |               4.816 |

Learned router vs try-except: +0.0 points kept, 11% less compute.

**Verdict for this profile:** PARTIAL: different meshes need different chains, but cheap features don't route well yet; focus on adaptive execution.


## Profile: cad (token + sharp edges)

993 meshes, 16 chains.

| chain                |   success % |   fail (timeout/oom/crash) % |   median time s |   p99 time s |   total time h |   median hausdorff |   median sharp recall |
|:---------------------|------------:|-----------------------------:|----------------:|-------------:|---------------:|-------------------:|----------------------:|
| ftetwild+dec+fix     |      18.429 |                       20.443 |          35.527 |      120.101 |         13.059 |              0.025 |                 0.798 |
| local+dec            |      16.616 |                        1.208 |           0.145 |       12.931 |          0.378 |              0.002 |                 0.998 |
| local+remesh         |      14.804 |                        5.136 |           1.207 |      120.1   |          3.535 |              0.005 |                 0.995 |
| cull+local+dec       |      12.588 |                        3.827 |           0.979 |      120.1   |          2.472 |              0.005 |                 0.946 |
| local                |      11.38  |                        1.007 |           0.08  |        3.883 |          0.105 |              0     |                 1     |
| cull+local           |       8.862 |                        3.726 |           0.58  |      120.1   |          2.224 |              0.003 |                 1     |
| manifoldplus+dec+fix |       4.532 |                        1.007 |           6.642 |       61.293 |          3.34  |              0.006 |                 0.467 |
| meshfix+dec          |       4.431 |                        4.431 |           0.528 |      120.098 |          2.497 |              0.039 |                 0.855 |
| meshfix              |       2.92  |                        4.632 |           0.353 |      120.1   |          2.123 |              0.035 |                 0.96  |
| wn256+dec            |       1.309 |                       13.494 |          16.705 |      120.101 |          9.902 |              0.024 |                 0.335 |
| dec+udf256+dec       |       0.101 |                        1.108 |           5.245 |       21.637 |          1.715 |              0.012 |                 0.022 |
| udf256+dec           |       0.101 |                        1.208 |           4.853 |       20.522 |          1.563 |              0.012 |                 0.021 |
| udf256+remesh        |       0.101 |                        2.518 |           6.265 |      120.039 |          4.525 |              0.016 |                 0.069 |
| udf128               |       0     |                        1.208 |           0.281 |        2.898 |          0.1   |              0.021 |                 0     |
| udf256               |       0     |                        1.511 |           2.641 |        7.1   |          0.781 |              0.011 |                 0     |
| wn256                |       0     |                       13.998 |          13.888 |      120.101 |          9.424 |              0.02  |                 0.599 |

**Q1 Silver bullet.** Best single chain: `ftetwild+dec+fix` (18.4% success). Per-mesh oracle: 29.2%. Gap: **10.8 points** (107 meshes only another chain can fix). `ftetwild+dec+fix` is the cheapest successful chain for 31% of meshes.

Filter-and-discard baseline (`local` then drop failures) keeps 11.4% of meshes.

**Q2 Heavy tails.** For `ftetwild+dec+fix`: slowest 1% of meshes take 3% of its total time, slowest 5% take 13%. p99/median = 3x.

**Q3 Predictable from cheap features?** Cross-validated AUC for "this chain succeeds": `cull+local` 0.94, `cull+local+dec` 0.89, `ftetwild+dec+fix` 0.86, `local` 0.95, `local+dec` 0.90, `local+remesh` 0.90, `manifoldplus+dec+fix` 0.83, `meshfix` 0.94, `meshfix+dec` 0.90, `wn256+dec` 0.92. (0.5 = random, >0.75 = useful.)

**Q4 Policies.**

| policy             |   kept % |   total compute h |   sec per kept mesh |
|:-------------------|---------:|------------------:|--------------------:|
| best single chain  |   18.429 |            13.059 |             256.89  |
| try-except cascade |   29.204 |            51.147 |             634.925 |
| learned router     |   29.204 |            49.652 |             616.374 |
| oracle             |   29.204 |             0.667 |               8.274 |

Learned router vs try-except: +0.0 points kept, 3% less compute.

**Verdict for this profile:** PARTIAL: different meshes need different chains, but cheap features don't route well yet; focus on adaptive execution.
