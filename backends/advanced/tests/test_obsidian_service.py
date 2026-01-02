import unittest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from advanced_omi_backend.services.obsidian_service import (
    ObsidianService,
    ObsidianSearchError,
)

class TestObsidianService(unittest.TestCase):

    def setUp(self):
        # Patch load_root_config
        self.config_patcher = patch('advanced_omi_backend.services.obsidian_service.load_root_config')
        self.mock_load_config = self.config_patcher.start()
        self.mock_load_config.return_value = {
            'defaults': {'llm': 'gpt-4', 'embedding': 'text-embedding-3-small'},
            'models': [
                {'name': 'gpt-4', 'model_url': 'https://api.openai.com/v1', 'api_key': 'sk-test'},
                {'name': 'text-embedding-3-small', 'model_name': 'text-embedding-3-small', 'embedding_dimensions': 1536, 'model_url': 'https://api.openai.com/v1', 'api_key': 'sk-test'}
            ]
        }
        self.addCleanup(self.config_patcher.stop)

        # Patch embedding helper
        self.embedding_patcher = patch(
            'advanced_omi_backend.services.obsidian_service.generate_openai_embeddings',
            new_callable=AsyncMock
        )
        self.mock_generate_embeddings = self.embedding_patcher.start()
        self.addCleanup(self.embedding_patcher.stop)

        # Patch GraphDatabase
        self.graph_db_patcher = patch('advanced_omi_backend.services.neo4j_client.GraphDatabase')
        self.mock_graph_db = self.graph_db_patcher.start()
        self.mock_driver = MagicMock()
        self.mock_session = MagicMock()
        self.mock_graph_db.driver.return_value = self.mock_driver
        self.mock_driver.session.return_value.__enter__.return_value = self.mock_session
        self.addCleanup(self.graph_db_patcher.stop)

        # Patch environment variables
        self.env_patcher = patch.dict(os.environ, {
            "NEO4J_HOST": "localhost",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "password"
        })
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        
        # Initialize Service
        self.service = ObsidianService()

    def test_search_obsidian_success(self):
        # Setup mock embedding response
        mock_embedding = [0.1, 0.2, 0.3]
        self.mock_generate_embeddings.return_value = [mock_embedding]
        
        # Setup mock Neo4j results
        mock_record1 = {
            'source': 'Note1',
            'content': 'Content of chunk 1',
            'tags': ['tag1', 'tag2'],
            'outgoing_links': ['Note2'],
            'score': 0.95
        }
        mock_record2 = {
            'source': 'Note2',
            'content': 'Content of chunk 2',
            'tags': [],
            'outgoing_links': [],
            'score': 0.90
        }
        
        # The session.run returns an iterable of records
        self.mock_session.run.return_value = [mock_record1, mock_record2]
        
        # Execute search
        response = asyncio.run(self.service.search_obsidian("test query", limit=2))
        
        # Assertions
        # 1. Check embedding call
        self.mock_generate_embeddings.assert_awaited_once()
        
        # 2. Check Neo4j query execution
        self.mock_session.run.assert_called_once()
        args, kwargs = self.mock_session.run.call_args
        self.assertIn("CALL db.index.vector.queryNodes", args[0])
        self.assertEqual(kwargs['vector'], mock_embedding)
        self.assertEqual(kwargs['limit'], 2)
        
        # 3. Check results formatting
        self.assertEqual(len(response['results']), 2)
        
        # Check first result format
        first_entry = response['results'][0]
        self.assertIn("SOURCE: Note1", first_entry)
        self.assertIn("TAGS: tag1, tag2", first_entry)
        self.assertIn("RELATED NOTES: Note2", first_entry)
        self.assertIn("CONTENT: Content of chunk 1", first_entry)

    def test_setup_database(self):
        self.service.setup_database()
        
        # Verify constraints and index creation calls
        self.assertTrue(self.mock_session.run.called)
        # It should run at least 3 queries: Note constraint, Chunk constraint, Vector Index
        self.assertGreaterEqual(self.mock_session.run.call_count, 3)
        
        calls = [call[0][0] for call in self.mock_session.run.call_args_list]
        self.assertTrue(any("CREATE CONSTRAINT note_path" in c for c in calls))
        self.assertTrue(any("CREATE CONSTRAINT chunk_id" in c for c in calls))
        self.assertTrue(any("CREATE VECTOR INDEX chunk_embeddings" in c for c in calls))

    @patch('advanced_omi_backend.services.obsidian_service.chunk_text_with_spacy')
    def test_chunking_and_embedding_uses_shared_chunker(self, mock_chunker):
        mock_chunker.return_value = ["part1"]
        self.mock_generate_embeddings.return_value = [[0.1, 0.2]]
        note_data = {
            "path": "x",
            "name": "n",
            "folder": "f",
            "content": "sample",
            "wordcount": 1,
            "links": [],
            "tags": [],
        }
        chunks = asyncio.run(self.service.chunking_and_embedding(note_data))
        mock_chunker.assert_called_once_with("sample", max_tokens=self.service.chunk_word_limit)
        self.mock_generate_embeddings.assert_awaited_once()
        self.assertEqual(len(chunks), 1)

    def test_ingest_note_and_chunks(self):
        note_data = {
            "path": "test/note.md",
            "name": "note",
            "folder": "test",
            "content": "some content",
            "wordcount": 2,
            "links": ["OtherNote"],
            "tags": ["tag1"]
        }
        chunks = [
            {"text": "chunk1", "embedding": [0.1, 0.2]}
        ]
        
        self.service.ingest_note_and_chunks(note_data, chunks)
        
        # Verify DB calls
        # 1. Note + Folder merge
        # 2. Chunk merge
        # 3. Tag merge
        # 4. Link merge
        self.assertGreaterEqual(self.mock_session.run.call_count, 4)
        
        calls = [call[0][0] for call in self.mock_session.run.call_args_list]
        self.assertTrue(any("MERGE (f:Folder" in c for c in calls))
        self.assertTrue(any("MERGE (c:Chunk" in c for c in calls))
        self.assertTrue(any("MERGE (t:Tag" in c for c in calls))
        self.assertTrue(any("MATCH (source:Note" in c for c in calls))

    def test_search_obsidian_embedding_fail(self):
        # Mock embedding failure (raises exception)
        self.mock_generate_embeddings.side_effect = Exception("API Error")
        
        with self.assertRaises(ObsidianSearchError) as ctx:
            asyncio.run(self.service.search_obsidian("test query"))
        
        self.assertEqual(ctx.exception.stage, "embedding")
        self.assertIn("API Error", str(ctx.exception))
        self.mock_session.run.assert_not_called()

    def test_search_obsidian_db_fail(self):
        # Setup mock embedding
        mock_embedding = [0.1]
        self.mock_generate_embeddings.return_value = [mock_embedding]
        
        # Mock DB failure
        self.mock_session.run.side_effect = Exception("DB Connection Failed")
        
        with self.assertRaises(ObsidianSearchError) as ctx:
            asyncio.run(self.service.search_obsidian("test query"))
        
        self.assertEqual(ctx.exception.stage, "database")
        self.assertIn("DB Connection Failed", str(ctx.exception))

    def test_search_obsidian_empty_results(self):
        # Setup mock embedding
        mock_embedding = [0.1]
        self.mock_generate_embeddings.return_value = [mock_embedding]
        
        # Mock empty DB results
        self.mock_session.run.return_value = []
        
        response = asyncio.run(self.service.search_obsidian("test query"))
        
        self.assertEqual(response['results'], [])

if __name__ == '__main__':
    unittest.main()
