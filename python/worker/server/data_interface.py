"""
Copyright 2022 Huawei Cloud Computing Technologies Co., Ltd.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

 http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import absolute_import
import warnings
from typing import Set

import pyarrow as pa
import numpy as np
import pandas as pd
from openGemini_udf.metadata import MetaData

from . import const as con

warnings.filterwarnings("ignore", category=np.VisibleDeprecationWarning)


class DataInterface:
    """
    in this class, the main objectives are
    1. transform the received data format from the kapacitor, i.e. pyarrow.RecordBatch
    into the castor readable format, i.e. pandas.DataFrame
    2. transform the data format generated by castor, i.e. pandas.DataFrame into
    the kapacitor readable format, i.e. pyarrow.RecordBatch

    in terms of tags, there are two sources of tag kv pairs
    1. group by tags, i.e. g_tag_kv: these are generated by the database group by operation
    and is stored in schema.metadata. these when returning the data from castor to the
    kapacitor, these tags must be unchanged. also, in one time series, there is only one
    tag value corresponding to each tag key. as a result of this one to one mapping between
    key and value, the group by tags are not carried in the pandas dataframes going into
    castor but are stored in self.gtag_dict

    2. extra tags. i.e. extra_tag_kv: these are tag keys and values which are the tags expect g_tag_kv.
    for extra tags, each tag key corresponds to only one tag value in the same way as g_tag_kv do.

    the instance attributes are:
    g_tag_kv: a dict, record the group by tags received from kapacitor
    extra_tag_kv: a dict, store the added tag kv pair when returning the data to kapacitor
    extra_field_kv: a dict, store the added field kv and dtype when returning the data to kapacitor
    """

    def __init__(self):
        self.g_tag_kv = dict()
        self.extra_tag_kv = dict()
        self.extra_field_kv = dict()
        self.series_key = None

    def _clear_data(self):
        self.g_tag_kv = dict()
        self.extra_tag_kv = dict()
        self.extra_field_kv = dict()
        self.series_key = None

    @staticmethod
    def _metadata_to_field_ids(
        task_id: bytes, tags: dict, columns: list, meta: MetaData
    ) -> list:
        """
        map list of meta data (task_id, tag, field_name) to field_ids
        """
        col_list = []
        tag = tuple((key, value) for key, value in tags.items())
        for i, col in enumerate(columns):
            meta_data = (task_id, tag, col.encode())
            col_list.append(meta.register_meta_data(meta_data))
        return col_list

    def arrow_to_pandas(
        self, batch: pa.RecordBatch, tags: dict, task_id: bytes, meta: MetaData
    ) -> pd.DataFrame:
        df_new = batch.to_pandas()
        df_new[con.DATA_TIME] = pd.to_datetime(
            df_new[con.DATA_TIME] // int(con.SEC_TO_NS), unit="s"
        )
        df_new.set_index(con.DATA_TIME, inplace=True)
        columns = df_new.columns
        df_new.columns = self._metadata_to_field_ids(task_id, tags, columns, meta)
        return df_new

    def _get_series_key(self, metadata: dict):
        self.series_key = ",".join(
            sorted(
                (
                    "=".join((key.decode(), value.decode()))
                    for key, value in metadata.items()
                )
            )
        )
        if self.series_key is None:
            self.series_key = "TEMP"

    @staticmethod
    def get_field_type(columns: list, df: pd.DataFrame) -> dict:
        mapping = {
            "str": pa.utf8(),
            "int64": pa.int64(),
            "float64": pa.float64(),
            "int32": pa.int64(),
            "float32": pa.float64(),
            "bool": pa.bool_(),
            "bool_": pa.bool_(),
        }
        type_kv = {con.DATA_TIME: pa.int64()}
        for column in columns:
            col_type = mapping.get(type(df[column].iloc[0]).__name__)
            mapping_keys = list(mapping.keys())
            if col_type is None:
                raise TypeError(
                    "The type of field is wrong, which should be in %s", mapping_keys
                )

            type_kv.update({column: col_type})

        return type_kv

    def add_tag(self, key: str, value: str):
        self.extra_tag_kv.update({key: value})

    def add_field(self, key: str, value, data_type: pa.DataType):
        self.extra_field_kv.update({key: (value, data_type)})

    @staticmethod
    def _create_arrow_with_metadata(metadata) -> pa.RecordBatch:
        metadata[con.ANOMALY_NUM] = str(0).encode()
        return pa.RecordBatch.from_pydict(mapping={}, metadata=metadata)

    @staticmethod
    def _datetime2timestamp(df: pd.DataFrame) -> pd.DataFrame:
        df.set_index(df.index.values.astype(np.int64), inplace=True)
        df.index.set_names(con.DATA_TIME, inplace=True)
        return df

    def pandas_to_arrow(self, df: pd.DataFrame = None) -> pa.RecordBatch:
        """
        convert dataframe or None to BatchRecord of arrow
        need tag_kv, field_type_kv and dataframe to create arrow
        tag_kv is a dict of string keys and values
        field_type_kv is a dict of string keys and types
        """
        tag_kv = self.g_tag_kv.copy()
        tag_kv.update(self.extra_tag_kv)

        if df is None:
            sub_df = pd.DataFrame()
            tag_kv.update({con.ANOMALY_NUM: str(0)})
            field_type_kv = {}
        else:
            sub_df = self._datetime2timestamp(df).copy()
            field_type_kv = self.get_field_type(list(df.columns), sub_df)
            field_type_kv.update(
                {key: value[1] for key, value in self.extra_field_kv.items()}
            )
            for key, value in self.extra_field_kv.items():
                sub_df[key] = value[0]

        sub_df.reset_index(inplace=True)
        # create the schema
        schema = pa.schema(field_type_kv, tag_kv)
        sub_batch = pa.RecordBatch.from_pandas(
            df=sub_df, schema=schema, preserve_index=False
        )
        sub_batch = sub_batch.replace_schema_metadata(schema.metadata)
        return sub_batch


class MetadataProcessor:
    """
    process metadata of input
    output_key: presents which keys will be added into metadata of output
    not_output_key: presents which keys won't be added into metadata of output
    special_keys: presents which keys is specified as meaningful keys,
        the keys is not in the special keys maybe tags of the time series or maybe unuseful keys
    not_necessary_for_stream, not_necessary_for_batch: presents which keys are not needed in input in stream or batch
        interface. The set of (special_keys - not_necessary_for_stream) or (special_keys - not_necessary_for_batch)
        presents which keys must by needed in input.
    """

    output_key = [
        con.DATA_ID,
        con.MSG_TYPE,
        con.CONN_ID,
        con.TASK_ID,
    ]

    not_output_key = [
        con.OUTPUT_INFO,
        con.ALGORITHM,
        con.CONFIG_FILE,
        con.PROCESS_TYPE,
        con.QUERY_MODE,
    ]
    not_necessary_for_stream = [con.OUTPUT_INFO, con.QUERY_MODE, con.PROCESS_TYPE]
    not_necessary_for_batch = [con.OUTPUT_INFO, con.QUERY_MODE]

    special_keys = output_key + not_output_key

    def __init__(self, info: dict) -> None:
        self._info = info
        # the metadata, which key is not in special_keys
        self._other_metadata = {}
        # the metadata, which key is in output_key
        self._output_metadata = {}

    def get_other_metadata(self) -> dict:
        return self._other_metadata

    def get_output_metadata(self) -> dict:
        return self._output_metadata

    def get_value(self, key: bytes):
        value = self._info.get(key)
        if value is None:
            return None
        return value.decode()

    def _filter(self, with_other_info: bool = False):
        for k, v in self._info.items():
            if with_other_info and k not in self.special_keys:
                self._other_metadata[k] = v
            if k in self.output_key:
                self._output_metadata[k] = v

    def _valid(self, keys: Set[bytes]):
        not_in_info = keys - set(self._info.keys())
        if len(not_in_info) > 0:
            raise KeyError(
                "Information is miss in metadata, %s are necessary" % list(not_in_info)
            )

    def process(self, mode: str = "batch"):
        if mode == "batch":
            necessary_keys = set(self.special_keys) - set(self.not_necessary_for_batch)
            with_other_info = True
        else:
            necessary_keys = set(self.special_keys) - set(self.not_necessary_for_stream)
            with_other_info = False
        self._validate_and_filter(necessary_keys, with_other_info)

    def _validate_and_filter(
        self, necessary_keys: Set[bytes], with_other_info: bool = False
    ) -> None:
        self._valid(necessary_keys)
        self._filter(with_other_info)