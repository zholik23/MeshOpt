# MeshOpt

MeshOpt studies how to prepare large collections of 3D meshes (such as Objaverse and Thingi10K) as training data for 3D generative models. Raw meshes are often broken: they have holes, overlapping or hidden parts, self-intersections and far too many faces. Different downstream models also need different things: implicit-field models need watertight shapes, mesh-token models need clean manifold meshes with a bounded face count, and CAD-style models also need sharp edges kept.

The project asks one question: **does the best repair method depend on the mesh and on what the data is for, or does a single method (a "silver bullet") work everywhere?** If it depends on the mesh, an optimizer that picks the right repair chain for each mesh from cheap features could keep more data at lower compute cost than a fixed pipeline.

This repository holds the pilot study behind that question. It runs a set of repair chains on every mesh, isolating each run with its own time and memory limits. The chains combine local cleanup (PyMeshLab, MeshFix), volumetric rebuilds (unsigned distance fields, generalized winding numbers, ManifoldPlus, fTetWild), hidden-part removal, decimation, remeshing and Draco compression. For each run it records success, cost and output quality, and the analysis then asks four things for each downstream use:

- Is one method best for nearly every mesh?
- Is compute dominated by a few slow meshes?
- Can cheap mesh features predict which method will fail?
- Would a learned router beat a fixed try-in-order fallback?
