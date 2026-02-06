"""Cypher query templates for Knowledge Graph operations.

This module contains all Cypher queries used by the KnowledgeGraphService
for CRUD operations on entities, relationships, and promises in Neo4j.
"""

# =============================================================================
# ENTITY QUERIES
# =============================================================================

CREATE_ENTITY = """
MERGE (e:Entity {id: $id})
SET e.name = $name,
    e.type = $type,
    e.user_id = $user_id,
    e.details = $details,
    e.icon = $icon,
    e.metadata = $metadata,
    e.created_at = datetime($created_at),
    e.updated_at = datetime($updated_at)
WITH e
CALL apoc.do.when(
    $type = 'person',
    'SET e:Person RETURN e',
    'RETURN e',
    {e: e}
) YIELD value AS v1
CALL apoc.do.when(
    $type = 'place',
    'SET e:Place, e.location = $location RETURN e',
    'RETURN e',
    {e: e, location: $location}
) YIELD value AS v2
CALL apoc.do.when(
    $type = 'organization',
    'SET e:Organization RETURN e',
    'RETURN e',
    {e: e}
) YIELD value AS v3
CALL apoc.do.when(
    $type = 'event',
    'SET e:Event, e.start_time = datetime($start_time), e.end_time = datetime($end_time) RETURN e',
    'RETURN e',
    {e: e, start_time: $start_time, end_time: $end_time}
) YIELD value AS v4
CALL apoc.do.when(
    $type = 'conversation',
    'SET e:Conversation, e.conversation_id = $conversation_id RETURN e',
    'RETURN e',
    {e: e, conversation_id: $conversation_id}
) YIELD value AS v5
CALL apoc.do.when(
    $type = 'promise',
    'SET e:Promise RETURN e',
    'RETURN e',
    {e: e}
) YIELD value AS v6
CALL apoc.do.when(
    $type = 'fact',
    'SET e:Fact RETURN e',
    'RETURN e',
    {e: e}
) YIELD value AS v7
RETURN e
"""

# Simpler version without APOC for basic installations
CREATE_ENTITY_SIMPLE = """
MERGE (e:Entity {id: $id})
SET e.name = $name,
    e.type = $type,
    e.user_id = $user_id,
    e.details = $details,
    e.icon = $icon,
    e.metadata = $metadata,
    e.created_at = datetime($created_at),
    e.updated_at = datetime($updated_at),
    e.location = $location,
    e.start_time = CASE WHEN $start_time IS NOT NULL THEN datetime($start_time) ELSE NULL END,
    e.end_time = CASE WHEN $end_time IS NOT NULL THEN datetime($end_time) ELSE NULL END,
    e.conversation_id = $conversation_id
RETURN e
"""

GET_ENTITY_BY_ID = """
MATCH (e:Entity {id: $id, user_id: $user_id})
OPTIONAL MATCH (e)-[r]-()
RETURN e, count(r) as relationship_count
"""

GET_ENTITIES_BY_USER = """
MATCH (e:Entity {user_id: $user_id})
WHERE $type IS NULL OR e.type = $type
OPTIONAL MATCH (e)-[r]-()
WITH e, count(r) as relationship_count
RETURN e, relationship_count
ORDER BY e.updated_at DESC
LIMIT $limit
"""

SEARCH_ENTITIES_BY_NAME = """
MATCH (e:Entity {user_id: $user_id})
WHERE toLower(e.name) CONTAINS toLower($query)
   OR ($query IS NOT NULL AND e.details IS NOT NULL AND toLower(e.details) CONTAINS toLower($query))
OPTIONAL MATCH (e)-[r]-()
WITH e, count(r) as relationship_count
RETURN e, relationship_count
ORDER BY e.updated_at DESC
LIMIT $limit
"""

FIND_ENTITY_BY_NAME = """
MATCH (e:Entity {user_id: $user_id})
WHERE toLower(e.name) = toLower($name)
RETURN e
LIMIT 1
"""

DELETE_ENTITY = """
MATCH (e:Entity {id: $id, user_id: $user_id})
DETACH DELETE e
RETURN count(e) as deleted_count
"""

UPDATE_ENTITY = """
MATCH (e:Entity {id: $id, user_id: $user_id})
SET e.name = COALESCE($name, e.name),
    e.details = COALESCE($details, e.details),
    e.icon = COALESCE($icon, e.icon),
    e.metadata = COALESCE($metadata, e.metadata),
    e.updated_at = datetime()
RETURN e
"""

# =============================================================================
# RELATIONSHIP QUERIES
# =============================================================================

CREATE_RELATIONSHIP = """
MATCH (source:Entity {id: $source_id, user_id: $user_id})
MATCH (target:Entity {id: $target_id, user_id: $user_id})
MERGE (source)-[r:RELATED_TO {id: $id}]->(target)
SET r.type = $type,
    r.user_id = $user_id,
    r.context = $context,
    r.timestamp = CASE WHEN $timestamp IS NOT NULL THEN datetime($timestamp) ELSE NULL END,
    r.start_date = CASE WHEN $start_date IS NOT NULL THEN datetime($start_date) ELSE NULL END,
    r.end_date = CASE WHEN $end_date IS NOT NULL THEN datetime($end_date) ELSE NULL END,
    r.metadata = $metadata,
    r.created_at = datetime($created_at)
RETURN r, source, target
"""

GET_ENTITY_RELATIONSHIPS = """
MATCH (e:Entity {id: $entity_id, user_id: $user_id})
OPTIONAL MATCH (e)-[r]->(target:Entity)
OPTIONAL MATCH (source:Entity)-[r2]->(e)
WITH e,
     collect(DISTINCT {rel: r, target: target, direction: 'outgoing'}) as outgoing,
     collect(DISTINCT {rel: r2, source: source, direction: 'incoming'}) as incoming
RETURN e, outgoing, incoming
"""

GET_RELATIONSHIPS_BETWEEN = """
MATCH (source:Entity {id: $source_id, user_id: $user_id})
MATCH (target:Entity {id: $target_id, user_id: $user_id})
MATCH (source)-[r]->(target)
RETURN r, source, target
"""

DELETE_RELATIONSHIP = """
MATCH ()-[r {id: $id}]->()
WHERE r.user_id = $user_id
DELETE r
RETURN count(r) as deleted_count
"""

# =============================================================================
# PROMISE QUERIES
# =============================================================================

CREATE_PROMISE = """
MERGE (p:Promise:Entity {id: $id})
SET p.user_id = $user_id,
    p.action = $action,
    p.name = $action,
    p.type = 'promise',
    p.to_entity_id = $to_entity_id,
    p.to_entity_name = $to_entity_name,
    p.status = $status,
    p.due_date = CASE WHEN $due_date IS NOT NULL THEN datetime($due_date) ELSE NULL END,
    p.completed_at = CASE WHEN $completed_at IS NOT NULL THEN datetime($completed_at) ELSE NULL END,
    p.source_conversation_id = $source_conversation_id,
    p.context = $context,
    p.metadata = $metadata,
    p.created_at = datetime($created_at),
    p.updated_at = datetime($updated_at)
WITH p
OPTIONAL MATCH (target:Entity {id: $to_entity_id, user_id: $user_id})
FOREACH (_ IN CASE WHEN target IS NOT NULL THEN [1] ELSE [] END |
    MERGE (p)-[:PROMISED_TO]->(target)
)
OPTIONAL MATCH (conv:Entity {conversation_id: $source_conversation_id, user_id: $user_id})
FOREACH (_ IN CASE WHEN conv IS NOT NULL THEN [1] ELSE [] END |
    MERGE (p)-[:EXTRACTED_FROM]->(conv)
)
RETURN p
"""

GET_PROMISES_BY_USER = """
MATCH (p:Promise {user_id: $user_id})
WHERE $status IS NULL OR p.status = $status
OPTIONAL MATCH (p)-[:PROMISED_TO]->(target:Entity)
RETURN p, target
ORDER BY
    CASE WHEN p.due_date IS NOT NULL THEN p.due_date ELSE datetime('9999-12-31') END ASC,
    p.created_at DESC
LIMIT $limit
"""

GET_PROMISE_BY_ID = """
MATCH (p:Promise {id: $id, user_id: $user_id})
OPTIONAL MATCH (p)-[:PROMISED_TO]->(target:Entity)
OPTIONAL MATCH (p)-[:EXTRACTED_FROM]->(conv:Entity)
RETURN p, target, conv
"""

UPDATE_PROMISE_STATUS = """
MATCH (p:Promise {id: $id, user_id: $user_id})
SET p.status = $status,
    p.updated_at = datetime(),
    p.completed_at = CASE WHEN $status = 'completed' THEN datetime() ELSE p.completed_at END
RETURN p
"""

DELETE_PROMISE = """
MATCH (p:Promise {id: $id, user_id: $user_id})
DETACH DELETE p
RETURN count(p) as deleted_count
"""

# =============================================================================
# TIMELINE QUERIES
# =============================================================================

GET_TIMELINE = """
MATCH (e:Entity {user_id: $user_id})
WHERE (e.start_time IS NOT NULL AND e.start_time >= datetime($start) AND e.start_time <= datetime($end))
   OR (e.created_at >= datetime($start) AND e.created_at <= datetime($end))
OPTIONAL MATCH (e)-[r]-()
WITH e, count(r) as relationship_count
RETURN e, relationship_count
ORDER BY COALESCE(e.start_time, e.created_at) ASC
LIMIT $limit
"""

# =============================================================================
# CONVERSATION ENTITY QUERIES
# =============================================================================

CREATE_CONVERSATION_ENTITY = """
MERGE (c:Conversation:Entity {conversation_id: $conversation_id, user_id: $user_id})
SET c.id = COALESCE(c.id, $id),
    c.name = $name,
    c.type = 'conversation',
    c.details = $details,
    c.metadata = $metadata,
    c.created_at = COALESCE(c.created_at, datetime($created_at)),
    c.updated_at = datetime($updated_at)
RETURN c
"""

LINK_ENTITY_TO_CONVERSATION = """
MATCH (e:Entity {id: $entity_id, user_id: $user_id})
MATCH (c:Conversation {conversation_id: $conversation_id, user_id: $user_id})
MERGE (e)-[r:MENTIONED_IN {id: $rel_id}]->(c)
SET r.timestamp = datetime($timestamp),
    r.context = $context,
    r.user_id = $user_id,
    r.created_at = datetime()
RETURN r
"""

GET_ENTITIES_FROM_CONVERSATION = """
MATCH (e:Entity)-[:MENTIONED_IN]->(c:Conversation {conversation_id: $conversation_id, user_id: $user_id})
RETURN e
ORDER BY e.name
"""

# =============================================================================
# GRAPH QUERIES
# =============================================================================

GET_ENTITY_GRAPH = """
MATCH (center:Entity {id: $entity_id, user_id: $user_id})
OPTIONAL MATCH path = (center)-[r*1..2]-(connected:Entity)
WHERE connected.user_id = $user_id
WITH center, collect(DISTINCT connected) as connected_nodes,
     collect(DISTINCT relationships(path)) as rels
RETURN center, connected_nodes, rels
"""

GET_USER_GRAPH = """
MATCH (e:Entity {user_id: $user_id})
OPTIONAL MATCH (e)-[r]->(e2:Entity {user_id: $user_id})
WITH collect(DISTINCT e) as nodes, collect(DISTINCT {source: startNode(r).id, target: endNode(r).id, type: type(r), properties: properties(r)}) as edges
RETURN nodes, edges
LIMIT $limit
"""

# =============================================================================
# CLEANUP QUERIES
# =============================================================================

DELETE_USER_ENTITIES = """
MATCH (e:Entity {user_id: $user_id})
DETACH DELETE e
RETURN count(e) as deleted_count
"""

DELETE_CONVERSATION_ENTITIES = """
MATCH (e:Entity)-[:MENTIONED_IN]->(c:Conversation {conversation_id: $conversation_id, user_id: $user_id})
DETACH DELETE e
WITH count(e) as entity_count
MATCH (c:Conversation {conversation_id: $conversation_id, user_id: $user_id})
DETACH DELETE c
RETURN entity_count + count(c) as deleted_count
"""
