# MeshOpt run report (repair + Draco)

1987 meshes analysed, 13 unreadable, 16 repair chains, Draco bits [10, 12, 14].

Repair status counts: {'ok': 30047, 'timeout': 1067, 'oom': 605, 'error': 73}


## A. Repair (Draco rows excluded)


## Summary

| profile   | best chain           |   best % |   oracle % |   mean AUC |
|:----------|:---------------------|---------:|-----------:|-----------:|
| sdf       | udf256               |   70.861 |     89.18  |      0.818 |
| token     | manifoldplus+dec+fix |   43.13  |     69.049 |      0.867 |
| cad       | ftetwild+dec+fix     |   17.061 |     29.24  |      0.927 |


### Profile `sdf`  (1987 meshes)

| chain                |   pass % |   unique fixes |   fail (timeout/oom/err) % |   median s |   total h |
|:---------------------|---------:|---------------:|---------------------------:|-----------:|----------:|
| udf256               |    70.86 |              5 |                       2.21 |       2.69 |      1.63 |
| udf256+dec           |    69.9  |              0 |                       1.71 |       4.98 |      3.19 |
| dec+udf256+dec       |    69.8  |              0 |                       1.51 |       5.31 |      3.49 |
| udf256+remesh        |    54.15 |              0 |                       2.52 |       5.31 |      7.68 |
| manifoldplus+dec+fix |    51.59 |             23 |                       1.51 |       6.73 |      6.92 |
| wn256                |    41.97 |              5 |                      15.15 |      13.74 |     18.83 |
| udf128               |    41.42 |              1 |                       1.76 |       0.29 |      0.22 |
| wn256+dec            |    41.02 |              1 |                      13.99 |      16.22 |     19.76 |
| ftetwild+dec+fix     |    34.98 |              7 |                      20.63 |      34.29 |     26.22 |
| local                |    31.86 |              0 |                       1.71 |       0.08 |      0.24 |
| local+dec            |    31.5  |              0 |                       1.61 |       0.16 |      0.77 |
| local+remesh         |    30.1  |              0 |                       4.48 |       0.98 |      5.83 |
| cull+local           |    26.67 |              0 |                       4.23 |       0.62 |      4.38 |
| cull+local+dec       |    26.37 |              0 |                       4.03 |       0.99 |      4.85 |
| meshfix              |    14.85 |              2 |                       5.74 |       0.36 |      4.39 |
| meshfix+dec          |    13.69 |              0 |                       5.03 |       0.58 |      5.07 |

Best single chain **`udf256` 70.9%**, per-mesh oracle **89.2%** (gap 18.3 points).

CatBoost cross-validated AUC ("chain passes"): mean **0.818**, min 0.725 (`wn256+dec`), max 0.909 (`meshfix+dec`).

| policy                          |   kept % |   compute h |
|:--------------------------------|---------:|------------:|
| always udf256                   |    70.86 |        1.63 |
| try all (cheapest first)        |    89.18 |       29.25 |
| router, skip chains with p<20%  |    86.91 |       10.51 |
| router, skip chains with p<10%  |    88.68 |       13.18 |
| router, skip chains with p<5%   |    88.98 |       14.65 |
| router, no skip                 |    89.18 |       22.6  |
| oracle (cheapest passing chain) |    89.18 |        1.06 |


### Profile `token`  (1987 meshes)

| chain                |   pass % |   unique fixes |   fail (timeout/oom/err) % |   median s |   total h |
|:---------------------|---------:|---------------:|---------------------------:|-----------:|----------:|
| manifoldplus+dec+fix |    43.13 |            143 |                       1.51 |       6.73 |      6.92 |
| udf256+dec           |    34.57 |              3 |                       1.71 |       4.98 |      3.19 |
| dec+udf256+dec       |    34.32 |              1 |                       1.51 |       5.31 |      3.49 |
| wn256+dec            |    28.49 |             28 |                      13.99 |      16.22 |     19.76 |
| ftetwild+dec+fix     |    27.93 |             17 |                      20.63 |      34.29 |     26.22 |
| local+dec            |    24.81 |             12 |                       1.61 |       0.16 |      0.77 |
| local+remesh         |    20.08 |              0 |                       4.48 |       0.98 |      5.83 |
| cull+local+dec       |    19.28 |              2 |                       4.03 |       0.99 |      4.85 |
| udf256+remesh        |    12.98 |              1 |                       2.52 |       5.31 |      7.68 |
| local                |    12.03 |              0 |                       1.71 |       0.08 |      0.24 |
| cull+local           |     9.26 |              0 |                       4.23 |       0.62 |      4.38 |
| meshfix+dec          |     8.1  |              4 |                       5.03 |       0.58 |      5.07 |
| meshfix              |     3.82 |              0 |                       5.74 |       0.36 |      4.39 |
| wn256                |     0.2  |              0 |                      15.15 |      13.74 |     18.83 |
| udf256               |     0.05 |              0 |                       2.21 |       2.69 |      1.63 |
| udf128               |     0    |              0 |                       1.76 |       0.29 |      0.22 |

Best single chain **`manifoldplus+dec+fix` 43.1%**, per-mesh oracle **69.0%** (gap 25.9 points).

CatBoost cross-validated AUC ("chain passes"): mean **0.867**, min 0.788 (`manifoldplus+dec+fix`), max 0.956 (`local`).

| policy                          |   kept % |   compute h |
|:--------------------------------|---------:|------------:|
| always manifoldplus+dec+fix     |    43.13 |        6.92 |
| try all (cheapest first)        |    69.05 |       65.44 |
| router, skip chains with p<20%  |    59.69 |        7.66 |
| router, skip chains with p<10%  |    65.88 |       13.68 |
| router, skip chains with p<5%   |    67.89 |       20.46 |
| router, no skip                 |    69.05 |       56.76 |
| oracle (cheapest passing chain) |    69.05 |        1.91 |


### Profile `cad`  (1987 meshes)

| chain                |   pass % |   unique fixes |   fail (timeout/oom/err) % |   median s |   total h |
|:---------------------|---------:|---------------:|---------------------------:|-----------:|----------:|
| ftetwild+dec+fix     |    17.06 |            135 |                      20.63 |      34.29 |     26.22 |
| local+dec            |    17.01 |             13 |                       1.61 |       0.16 |      0.77 |
| local+remesh         |    14.9  |              9 |                       4.48 |       0.98 |      5.83 |
| cull+local+dec       |    12.63 |              2 |                       4.03 |       0.99 |      4.85 |
| local                |    11.83 |              0 |                       1.71 |       0.08 |      0.24 |
| cull+local           |     8.66 |              0 |                       4.23 |       0.62 |      4.38 |
| manifoldplus+dec+fix |     5.08 |             29 |                       1.51 |       6.73 |      6.92 |
| meshfix+dec          |     4.73 |              2 |                       5.03 |       0.58 |      5.07 |
| meshfix              |     3.12 |              0 |                       5.74 |       0.36 |      4.39 |
| wn256+dec            |     1.41 |              0 |                      13.99 |      16.22 |     19.76 |
| dec+udf256+dec       |     0.2  |              0 |                       1.51 |       5.31 |      3.49 |
| udf256+dec           |     0.2  |              0 |                       1.71 |       4.98 |      3.19 |
| udf256+remesh        |     0.15 |              0 |                       2.52 |       5.31 |      7.68 |
| udf128               |     0    |              0 |                       1.76 |       0.29 |      0.22 |
| udf256               |     0    |              0 |                       2.21 |       2.69 |      1.63 |
| wn256                |     0    |              0 |                      15.15 |      13.74 |     18.83 |

Best single chain **`ftetwild+dec+fix` 17.1%**, per-mesh oracle **29.2%** (gap 12.2 points).

CatBoost cross-validated AUC ("chain passes"): mean **0.927**, min 0.887 (`manifoldplus+dec+fix`), max 0.959 (`meshfix`).

| policy                          |   kept % |   compute h |
|:--------------------------------|---------:|------------:|
| always ftetwild+dec+fix         |    17.06 |       26.22 |
| try all (cheapest first)        |    29.24 |      100.29 |
| router, skip chains with p<20%  |    22.75 |        1.97 |
| router, skip chains with p<10%  |    24.71 |        3.73 |
| router, skip chains with p<5%   |    26.52 |        6.31 |
| router, no skip                 |    29.24 |       97.06 |
| oracle (cheapest passing chain) |    29.24 |        0.94 |


## B. Draco compression of repaired outputs


1973 meshes, 90141 Draco rows (status: {'ok': 90141}).

**Size and quality**

|   bits |   median raw KB |   median Draco KB |   median ratio |   total size vs raw % |   |dHausdorff| p50 |   |dHausdorff| p99 |   lost watertight % |   lost token pass |   gained token pass |
|-------:|----------------:|------------------:|---------------:|----------------------:|-------------------:|-------------------:|--------------------:|------------------:|--------------------:|
|     10 |         175.805 |           10.9111 |        17.4727 |                2.8704 |             0.0001 |             0.0054 |              2.4728 |               151 |                  57 |
|     12 |         175.805 |           14.707  |        12.8561 |                3.8865 |             0      |             0.0054 |              2.4728 |               137 |                  56 |
|     14 |         175.805 |           18.4658 |        10.0189 |                4.6625 |             0      |             0.0055 |              2.4728 |               137 |                  58 |

**Cost** (repair for comparison: median 2.90 s, 2391 MB peak)

|   bits |   encode s p50 |   encode s max |   decode s p50 |   decode s max |   extra RAM MB p50 |   extra RAM MB p99 |   extra RAM MB max |
|-------:|---------------:|---------------:|---------------:|---------------:|-------------------:|-------------------:|-------------------:|
|     10 |         0.0034 |         1.3546 |         0.0007 |         0.2802 |             1.8438 |            22.4891 |            337.172 |
|     12 |         0.0024 |         1.3233 |         0.0007 |         0.2784 |             0      |            41.575  |            335.461 |
|     14 |         0.0025 |         1.3533 |         0.0007 |         0.3281 |             0      |             0.0412 |            306.223 |

**Watertightness lost after Draco, by chain** (rows per bit setting):

| base                 |   outputs |
|:---------------------|----------:|
| ftetwild+dec+fix     |       452 |
| udf256+remesh        |       226 |
| manifoldplus+dec+fix |        19 |
| local+remesh         |        17 |
| cull+local           |        10 |
| local                |         7 |
| cull+local+dec       |         6 |
| local+dec            |         6 |

**Per-mesh oracle after compression**

| profile   |   no Draco % |   10 bits % |   12 bits % |   14 bits % |
|:----------|-------------:|------------:|------------:|------------:|
| sdf       |         89.2 |        88.9 |        89   |        89   |
| token     |         69   |        68.4 |        68.7 |        68.7 |
| cad       |         29.2 |        28.2 |        28.2 |        28.4 |

**Lowest bits that keep a token-passing output passing** (5545 outputs): 10 bits: 5394, none: 130, 12 bits: 20, 14 bits: 1. Adaptive bits = 59% of the bytes of fixed 14 bits; fixed 10 bits = 58%.
