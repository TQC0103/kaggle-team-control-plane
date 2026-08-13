# Kaggle smoke test

This is the smallest real Kaggle workload for checking one account end to end.
The control plane copies this directory, replaces the metadata owner and kernel
slug with the explicitly assigned account, and then uploads the staged copy.
The checked-in files are never modified.

The script uses CPU, does not require Internet or datasets, prints one JSON
record, and writes `result.json` to `/kaggle/working`. After Kaggle completes,
the control plane downloads that file into its managed artifact directory.

To use it with the default secure source boundary, copy the whole directory
under the `KCP_ALLOWED_SOURCE_ROOT` configured for the real backend. Do not add
credentials, datasets, or private files to this example directory.
