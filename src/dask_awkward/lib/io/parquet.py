import abc
import itertools
import math
import operator

import fsspec
import pyarrow.parquet as pq
from awkward._v2.operations import ak_from_parquet, from_buffers, to_arrow_table
from awkward._v2.operations.ak_from_parquet import _load
from dask.base import tokenize
from dask.blockwise import BlockIndex
from dask.highlevelgraph import HighLevelGraph
from fsspec.core import get_fs_token_paths

from dask_awkward.lib.core import map_partitions, new_scalar_object, typetracer_array
from dask_awkward.lib.io.io import from_map


class _FromParquetFn:
    def __init__(self, columns=None, schema=None):
        self.columns = columns
        self.schema = schema

    @abc.abstractmethod
    def __call__(self, source):
        ...


class _FromParquetFileWiseFn(_FromParquetFn):
    def __init__(self, fs, columns, schema):
        super().__init__(columns=columns, schema=schema)
        self.fs = fs

    def __call__(self, source):
        return _file_to_partition(
            source,
            self.fs,
            self.columns,
            self.schema,
        )


class _FromParquetFragmentWiseFn(_FromParquetFn):
    def __init__(self, fs, columns, schema):
        super().__init__(columns=columns, schema=schema)
        self.fs = fs

    def __call__(self, pair):
        subrg, source = pair
        if isinstance(subrg, int):
            subrg = [[subrg]]
        return _file_to_partition(
            source, self.fs, self.columns, self.schema, subrg=subrg
        )


def from_parquet(
    path,
    storage_options=None,
    ignore_metadata=True,
    scan_files=False,
    columns=None,
    filters=None,
    split_row_groups=None,
):
    """Read parquet dataset into awkward array collection.

    url: str
        location of data, including protocol
    storage_options: dict
        for creating filesystem
    columns: list[str] or None
        Select columns to load
    filters: list[list[tuple]]
        parquet-style filters for excluding row groups based on column statistics
    split_row_groups: bool | None
        If True, each row group becomes a partition. If False, each file becomes
        a partition. If None, the existence of a `_metadata` file and
        ignore_metadata=False implies True, else False.
    """
    fs, tok, paths = get_fs_token_paths(
        path, mode="rb", storage_options=storage_options
    )
    label = "read-parquet"
    token = tokenize(
        tok, ignore_metadata, columns, filters, scan_files, split_row_groups
    )

    # same as ak_metadata_from_parquet
    results = ak_from_parquet.metadata(
        path,
        storage_options,
        row_groups=None,
        columns=columns,
        ignore_metadata=ignore_metadata,
        scan_files=scan_files,
    )
    parquet_columns, subform, actual_paths, fs, subrg, row_counts, metadata = results
    if split_row_groups is None:
        split_row_groups = row_counts is not None and len(row_counts) > 1

    meta = from_buffers(
        subform,
        length=0,
        container={"": b"\x00\x00\x00\x00\x00\x00\x00\x00"},
        buffer_key="",
    )

    if split_row_groups is False or subrg is None:
        # file-wise
        return from_map(
            _FromParquetFileWiseFn(
                fs,
                columns,
                subform,
            ),
            actual_paths,
            label=label,
            token=token,
            meta=typetracer_array(meta),
        )
    else:
        # row-group wise

        if set(subrg) == {None}:
            rgs_paths = {path: 0 for path in actual_paths}
            for i in range(metadata.num_row_groups):
                fp = metadata.row_group(i).column(0).file_path
                rgs_path = [p for p in rgs_paths if fp in p][
                    0
                ]  # returns 1st if fp is empty
                rgs_paths[rgs_path] += 1

            subrg = [list(range(i)) for _ in actual_paths]

        rgs = [metadata.row_group(i) for i in range(metadata.num_row_groups)]
        divisions = [0] + list(
            itertools.accumulate([rg.num_rows for rg in rgs], operator.add)
        )
        pairs = []
        for rgs, path in zip(subrg, actual_paths):
            pairs.extend([(rg, path) for rg in rgs])
        return from_map(
            _FromParquetFragmentWiseFn(
                fs,
                columns,
                subform,
            ),
            pairs,
            label=label,
            token=token,
            divisions=tuple(divisions),
            meta=typetracer_array(meta),
        )


def _file_to_partition(path, fs, columns, schema, subrg=None):
    """read a whole parquet file to awkward"""
    return _load(
        actual_paths=[path],
        fs=fs,
        parquet_columns=columns,
        subrg=subrg or [None],
        footer_sample_size=2**15,
        max_gap=2**10,
        max_block=2**22,
        generate_bitmasks=False,
        metadata=None,
        highlevel=True,
        subform=schema,
        behavior=None,
    )


def _metadata_file_from_data_files(path_list, fs, out_path):
    """
    Aggregate _metadata and _common_metadata from data files

    Maybe only used in testing

    (similar to fastparquet's merge)

    path_list: list[str]
        Input data files
    fs: AbstractFileSystem instance
    out_path: str
        Root directory of the dataset
    """
    meta = None
    out_path = out_path.rstrip("/")
    for path in path_list:
        assert path.startswith(out_path)
        with fs.open(path, "rb") as f:
            _meta = pq.ParquetFile(f).metadata
        _meta.set_file_path(path[len(out_path) + 1 :])
        if meta:
            meta.append_row_groups(_meta)
        else:
            meta = _meta
    _write_metadata(fs, out_path, meta)


def _metadata_file_from_metas(fs, out_path, *metas):
    """Agregate metadata from arrow objects and write"""
    meta = metas[0]
    for _meta in metas[1:]:
        meta.append_row_groups(_meta)
    _write_metadata(fs, out_path, meta)


def _write_metadata(fs, out_path, meta):
    """Output metadata files"""
    metadata_path = "/".join([out_path, "_metadata"])
    with fs.open(metadata_path, "wb") as fil:
        meta.write_metadata_file(fil)
    metadata_path = "/".join([out_path, "_metadata"])
    with fs.open(metadata_path, "wb") as fil:
        meta.write_metadata_file(fil)


def _write_partition(
    data,
    path,  # dataset root
    fs,
    filename,  # relative path within the dataset
    # partition_on=Fa,  # must be top-level leaf (i.e., a simple column)
    return_metadata=False,  # whether making global _metadata
    compression=None,  # TBD
    head=False,  # is this the first piece
    # custom_metadata=None,
):
    t = to_arrow_table(
        data,
        list_to32=True,
        string_to32=True,
        bytestring_to32=True,
        categorical_as_dictionary=True,
        extensionarray=False,
    )
    md_list = []
    with fs.open(fs.sep.join([path, filename]), "wb") as fil:
        pq.write_table(
            t,
            fil,
            compression=compression,
            metadata_collector=md_list,
        )

    # Return the schema needed to write global _metadata
    if return_metadata:
        _meta = md_list[0]
        _meta.set_file_path(filename)
        d = {"meta": _meta}
        if head:
            # Only return schema if this is the "head" partition
            d["schema"] = t.schema
        return [d]
    else:
        return []


class _ToParquetFn:
    def __init__(
        self,
        fs,
        path,
        return_metadata=False,
        compression=None,
        head=None,
        npartitions=None,
    ):
        self.fs = fs
        self.path = path
        self.return_metadata = return_metadata
        self.compression = compression
        self.head = head
        self.zfill = (
            math.ceil(math.log(npartitions, 10)) if npartitions is not None else 1
        )

    def __call__(self, data, block_index):
        filename = f"part{str(block_index[0]).zfill(self.zfill)}.parquet"
        return _write_partition(
            data,
            self.path,
            self.fs,
            filename,
            return_metadata=self.return_metadata,
            compression=self.compression,
            head=self.head,
        )


def to_parquet(data, path, storage_options=None, write_metadata=False, compute=True):
    """Write data to parquet format

    Parameters
    ----------
    data: DaskAwrkardArray
    path: str
        Root directory of location to write to
    storage_options: dict
        arguments to pass to fsspec for creating the filesystem
    write_metadata: bool
        Whether to create _metadata and _common_metadata files
    compute: bool
        Whether to immediately start writing or to return the dask
        collection which can be computed at the user's discression.

    Returns
    -------
    If compute=False, a dask Scalar representing the process
    """
    # TODO options we need:
    #  - compression per data type or per leaf column ("path.to.leaf": "zstd" format)
    #  - byte stream split for floats if compression is not None or lzma
    #  - partitioning
    #  - parquet 2 for full set of time and int types
    #  - v2 data page (for possible later fastparquet implementation)
    #  - dict encoding always off
    fs, _ = fsspec.core.url_to_fs(path, **(storage_options or {}))
    name = f"write-parquet-{tokenize(fs, data, path)}"

    map_res = map_partitions(
        _ToParquetFn(fs, path=path, npartitions=data.npartitions),
        data,
        BlockIndex((data.npartitions,)),
        label="to-parquet",
        meta=data._meta,
    )

    dsk = {}
    if write_metadata:
        final_name = name + "-metadata"
        dsk[(final_name, 0)] = (_metadata_file_from_metas, fs, path) + tuple(
            map_res.__dask_keys__()
        )
    else:
        final_name = name + "-finalize"
        dsk[(final_name, 0)] = (lambda *_: None, map_res.__dask_keys__())
    graph = HighLevelGraph.from_collections(final_name, dsk, dependencies=[map_res])
    out = new_scalar_object(graph, final_name, meta=None)
    if compute:
        out.compute()
    else:
        return out
