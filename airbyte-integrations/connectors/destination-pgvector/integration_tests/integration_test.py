#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#
from __future__ import annotations

import json
import logging
from typing import Any

import psycopg2
from airbyte_cdk.destinations.vector_db_based.test_utils import BaseIntegrationTest
from airbyte_cdk.models import DestinationSyncMode, Status

from destination_pgvector.destination import DestinationPGVector


class PGVectorIntegrationTest(BaseIntegrationTest):
    def setUp(self):
        with open("secrets/config.json", "r") as f:
            self.config = json.loads(f.read())

    def tearDown(self):
        pass

    def test_check_valid_config(self):
        outcome = DestinationPGVector().check(logging.getLogger("airbyte"), self.config)
        assert outcome.status == Status.SUCCEEDED

    def test_check_invalid_config(self):
        outcome = DestinationPGVector().check(
            logging.getLogger("airbyte"),
            {
                "processing": {
                    "text_fields": ["str_col"],
                    "chunk_size": 1000,
                    "metadata_fields": ["int_col"],
                },
                "embedding": {"mode": "openai", "openai_key": "mykey"},
                "indexing": {
                    "host": "MYACCOUNT",
                    "role": "MYUSERNAME",
                    "warehouse": "MYWAREHOUSE",
                    "database": "MYDATABASE",
                    "default_schema": "MYSCHEMA",
                    "username": "MYUSERNAME",
                    "credentials": {"password": "xxxxxxx"},
                },
            },
        )
        assert outcome.status == Status.FAILED

    def _get_db_connection(self):
        return psycopg2.connect(
            dbname=self.config["indexing"]["database"],
            user=self.config["indexing"]["username"],
            password=self.config["indexing"]["credentials"]["password"],
            host=self.config["indexing"]["host"],
            port=self.config["indexing"]["port"],
        )

    def _get_record_count(self, table_name):
        """Return the number of records in the table."""
        conn = self._get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table_name};")
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        return result[0]

    def _get_all_records(self, table_name) -> list[dict[str, Any]]:
        """Return all records from the table as a list of dictionaries."""
        conn = self._get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table_name};")
        column_names = [desc[0] for desc in cursor.description]
        result: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            result.append(dict(zip(column_names, row)))
        cursor.close()
        conn.close()
        return result

    def _delete_table(self, table_name):
        conn = self._get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {table_name};")
        conn.commit()
        conn.close()

    def _run_cosine_similarity(self, query_vector, table_name):
        conn = self._get_db_connection()
        cursor = conn.cursor()

        query = f"""
        SELECT DOCUMENT_CONTENT
        FROM {table_name}
        ORDER BY VECTOR_L2_DISTANCE(
            CAST({query_vector} AS VECTOR(FLOAT, 1536)),
            embedding
        )
        LIMIT 1
        """
        cursor.execute(query)
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        return result

    def test_write(self):
        self._delete_table("mystream")
        catalog = self._get_configured_catalog(DestinationSyncMode.overwrite)
        first_state_message = self._state({"state": "1"})
        first_record = [
            self._record(
                stream="mystream",
                str_value=f"Dogs are number {i}",
                int_value=i,
            )
            for i in range(5)
        ]

        # initial sync with replace
        destination = DestinationPGVector()
        _ = list(
            destination.write(
                config=self.config,
                configured_catalog=catalog,
                input_messages=[*first_record, first_state_message],
            )
        )
        assert self._get_record_count("mystream") == 5

        # subsequent sync with append
        append_catalog = self._get_configured_catalog(DestinationSyncMode.append)
        list(
            destination.write(
                config=self.config,
                configured_catalog=append_catalog,
                input_messages=[self._record("mystream", "Cats are nice", 6), first_state_message],
            )
        )
        assert self._get_record_count("mystream") == 6

    def test_write_and_replace(self):
        self._delete_table("mystream")
        catalog = self._get_configured_catalog(DestinationSyncMode.overwrite)
        first_state_message = self._state({"state": "1"})
        first_five_records = [
            self._record(
                stream="mystream",
                str_value=f"Dogs are number {i}",
                int_value=i,
            )
            for i in range(5)
        ]

        # initial sync with replace
        destination = DestinationPGVector()
        list(
            destination.write(
                config=self.config,
                configured_catalog=catalog,
                input_messages=[*first_five_records, first_state_message],
            )
        )
        assert self._get_record_count("mystream") == 5

        # subsequent sync with append
        append_catalog = self._get_configured_catalog(DestinationSyncMode.append)
        list(
            destination.write(
                config=self.config,
                configured_catalog=append_catalog,
                input_messages=[self._record("mystream", "Cats are nice", 6), first_state_message],
            )
        )
        assert self._get_record_count("mystream") == 6

        # subsequent sync with append_dedup
        append_dedup_catalog = self._get_configured_catalog(DestinationSyncMode.append_dedup)
        list(
            destination.write(
                config=self.config,
                configured_catalog=append_dedup_catalog,
                input_messages=[
                    self._record("mystream", "Cats are nice too", 4),
                    first_state_message,
                ],
            )
        )

        # TODO: FIXME: This should be 6, but it's 7 because the deduplication is not working
        assert self._get_record_count("mystream") == 6

        # comment the following so we can use fake for testing
        # embeddings = OpenAIEmbeddings(openai_api_key=self.config["embedding"]["openai_key"])
        # result = self._run_cosine_similarity(embeddings.embed_query("feline animals"), "mystream")
        # assert(len(result) == 1)
        # result[0] == "str_col: Cats are nice"

    def test_overwrite_mode_deletes_records(self):
        self._delete_table("mystream")
        catalog = self._get_configured_catalog(DestinationSyncMode.overwrite)
        first_state_message = self._state({"state": "1"})
        first_four_records = [
            self._record(
                stream="mystream",
                str_value=f"Dogs are number {i}",
                int_value=i,
            )
            for i in range(4)
        ]

        # initial sync with replace
        destination = DestinationPGVector()
        list(destination.write(self.config, catalog, [*first_four_records, first_state_message]))
        assert self._get_record_count("mystream") == 4

        # following should replace existing records
        append_catalog = self._get_configured_catalog(DestinationSyncMode.overwrite)
        list(
            destination.write(
                config=self.config,
                configured_catalog=append_catalog,
                input_messages=[self._record("mystream", "Cats are nice", 6), first_state_message],
            )
        )
        assert self._get_record_count("mystream") == 1

    def test_record_write_fidelity(self):
        self._delete_table("mystream")
        catalog = self._get_configured_catalog(DestinationSyncMode.overwrite)
        first_state_message = self._state({"state": "1"})
        records = [
            self._record(
                stream="mystream",
                str_value=f"Dogs are number {i}",
                int_value=i,
            )
            for i in range(1)
        ]

        # initial sync with replace
        destination = DestinationPGVector()
        list(destination.write(self.config, catalog, [*records, first_state_message]))
        assert self._get_record_count("mystream") == 1
        first_written_record = self._get_all_records("mystream")[0]
        assert list(first_written_record.keys()) == [
            "document_id",
            "chunk_id",
            "metadata",
            "document_content",
            "embedding",
        ]
        assert first_written_record.pop("embedding")
        assert first_written_record.pop("chunk_id")
        metadata = first_written_record.pop("metadata")
        _ = metadata

        # TODO: Fix the data type issue here (currently stringified):
        # assert isinstance(metadata, dict), f"METADATA should be a dict: {metadata}"
        # assert metadata["int_col"] == 0

        assert first_written_record == {
            "document_id": "Stream_mystream_Key_0",
            "document_content": "str_col: Dogs are number 0",
        }

    def test_write_with_chunk_size_5(self):
        self._delete_table("mystream")
        self.config["processing"]["chunk_size"] = 5
        catalog = self._get_configured_catalog(DestinationSyncMode.overwrite)
        first_state_message = self._state({"state": "1"})
        first_record = [
            self._record(
                stream="mystream",
                str_value=f"Dogs are number {i}",
                int_value=i,
            )
            for i in range(5)
        ]

        # initial sync with replace
        destination = DestinationPGVector()
        _ = list(
            destination.write(
                config=self.config,
                configured_catalog=catalog,
                input_messages=[*first_record, first_state_message],
            )
        )
        assert self._get_record_count("mystream") == 15

        # subsequent sync with append
        append_catalog = self._get_configured_catalog(DestinationSyncMode.append)
        list(
            destination.write(
                config=self.config,
                configured_catalog=append_catalog,
                input_messages=[self._record("mystream", "Cats are nice", 6), first_state_message],
            )
        )
        assert self._get_record_count("mystream") == 18

        # subsequent sync with append_dedup
        append_dedup_catalog = self._get_configured_catalog(DestinationSyncMode.append_dedup)
        list(
            destination.write(
                config=self.config,
                configured_catalog=append_dedup_catalog,
                input_messages=[
                    self._record("mystream", "Cats are nice too", 4),
                    first_state_message,
                ],
            )
        )
        assert self._get_record_count("mystream") == 18

    def test_write_fidelity_with_chunk_size_5(self):
        self._delete_table("mystream")
        self.config["processing"]["chunk_size"] = 5
        catalog = self._get_configured_catalog(DestinationSyncMode.overwrite)
        first_state_message = self._state({"state": "1"})
        records = [
            self._record(
                stream="mystream",
                str_value=f"Dogs are number {i}",
                int_value=i,
            )
            for i in range(1)
        ]

        # initial sync with replace
        destination = DestinationPGVector()
        list(destination.write(self.config, catalog, [*records, first_state_message]))
        assert self._get_record_count("mystream") == 3
        first_written_record = self._get_all_records("mystream")[0]
        second_written_record = self._get_all_records("mystream")[1]
        third_written_record = self._get_all_records("mystream")[2]
        assert list(first_written_record.keys()) == [
            "document_id",
            "chunk_id",
            "metadata",
            "document_content",
            "embedding",
        ]
        assert first_written_record.pop("embedding")
        assert first_written_record.pop("chunk_id")
        metadata = first_written_record.pop("metadata")
        _ = metadata

        assert first_written_record == {
            "document_id": "Stream_mystream_Key_0",
            "document_content": "str_col:",
        }
        assert second_written_record["document_id"] == "Stream_mystream_Key_0"
        assert second_written_record["document_content"] == "Dogs are"
        assert third_written_record["document_id"] == "Stream_mystream_Key_0"
        assert third_written_record["document_content"] == "number 0"
