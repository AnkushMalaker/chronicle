"""Cypher query templates for Knowledge Graph operations.

This module contains all Cypher queries used by the KnowledgeGraphService
for CRUD operations on entities and relationships in FalkorDB.

Each user's data lives in its own per-user graph (``chronicle_<user_id>``),
so these queries do not filter by ``user_id`` — the graph itself is the
isolation boundary. ``user_id`` is still written to nodes as a forensic
breadcrumb, but it is never read back as a filter predicate.
"""

# =============================================================================
# ENTITY QUERIES
# =============================================================================

CREATE_ENTITY_SIMPLE = """
MERGE (e:Entity {id: $id})
SET e.name = $name,
    e.type = $type,
    e.user_id = $user_id,
    e.details = $details,
    e.icon = $icon,
    e.metadata = $metadata,
    e.created_at = $created_at,
    e.updated_at = $updated_at,
    e.location = $location,
    e.start_time = CASE WHEN $start_time IS NOT NULL THEN $start_time ELSE NULL END,
    e.end_time = CASE WHEN $end_time IS NOT NULL THEN $end_time ELSE NULL END,
    e.conversation_id = $conversation_id
RETURN e
"""

GET_ENTITY_BY_ID = """
MATCH (e:Entity {id: $id})
OPTIONAL MATCH (e)-[r]-()
RETURN e, count(r) as relationship_count
"""

GET_ENTITIES_BY_USER = """
MATCH (e:Entity)
WHERE $type IS NULL OR e.type = $type
OPTIONAL MATCH (e)-[r]-()
WITH e, count(r) as relationship_count
RETURN e, relationship_count
ORDER BY e.updated_at DESC
LIMIT $limit
"""

SEARCH_ENTITIES_BY_NAME = """
MATCH (e:Entity)
WHERE toLower(e.name) CONTAINS toLower($query)
   OR ($query IS NOT NULL AND e.details IS NOT NULL AND toLower(e.details) CONTAINS toLower($query))
OPTIONAL MATCH (e)-[r]-()
WITH e, count(r) as relationship_count
RETURN e, relationship_count
ORDER BY e.updated_at DESC
LIMIT $limit
"""

FIND_ENTITY_BY_NAME = """
MATCH (e:Entity)
WHERE toLower(e.name) = toLower($name)
RETURN e
LIMIT 1
"""

DELETE_ENTITY = """
MATCH (e:Entity {id: $id})
DETACH DELETE e
RETURN count(e) as deleted_count
"""

UPDATE_ENTITY = """
MATCH (e:Entity {id: $id})
SET e.name = COALESCE($name, e.name),
    e.details = COALESCE($details, e.details),
    e.icon = COALESCE($icon, e.icon),
    e.metadata = COALESCE($metadata, e.metadata),
    e.updated_at = $now
RETURN e
"""

# =============================================================================
# RELATIONSHIP QUERIES
# =============================================================================

CREATE_RELATIONSHIP = """
MATCH (source:Entity {id: $source_id})
MATCH (target:Entity {id: $target_id})
MERGE (source)-[r:RELATED_TO {id: $id}]->(target)
SET r.type = $type,
    r.user_id = $user_id,
    r.context = $context,
    r.timestamp = $timestamp,
    r.start_date = $start_date,
    r.end_date = $end_date,
    r.metadata = $metadata,
    r.created_at = $created_at
RETURN r, source, target
"""

GET_ENTITY_RELATIONSHIPS = """
MATCH (e:Entity {id: $entity_id})
OPTIONAL MATCH (e)-[r]->(target:Entity)
OPTIONAL MATCH (source:Entity)-[r2]->(e)
WITH e,
     collect(DISTINCT {rel: r, target: target, direction: 'outgoing'}) as outgoing,
     collect(DISTINCT {rel: r2, source: source, direction: 'incoming'}) as incoming
RETURN e, outgoing, incoming
"""

GET_RELATIONSHIPS_BETWEEN = """
MATCH (source:Entity {id: $source_id})
MATCH (target:Entity {id: $target_id})
MATCH (source)-[r]->(target)
RETURN r, source, target
"""

DELETE_RELATIONSHIP = """
MATCH ()-[r {id: $id}]->()
DELETE r
RETURN count(r) as deleted_count
"""

# =============================================================================
# TIMELINE QUERIES
# =============================================================================

GET_TIMELINE = """
MATCH (e:Entity)
WHERE (e.start_time IS NOT NULL AND e.start_time >= $start AND e.start_time <= $end)
   OR (e.created_at >= $start AND e.created_at <= $end)
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
MERGE (c:Conversation:Entity {conversation_id: $conversation_id})
SET c.id = COALESCE(c.id, $id),
    c.name = $name,
    c.user_id = $user_id,
    c.type = 'conversation',
    c.details = $details,
    c.metadata = $metadata,
    c.created_at = COALESCE(c.created_at, $created_at),
    c.updated_at = $updated_at
RETURN c
"""

LINK_ENTITY_TO_CONVERSATION = """
MATCH (e:Entity {id: $entity_id})
MATCH (c:Conversation {conversation_id: $conversation_id})
MERGE (e)-[r:MENTIONED_IN {id: $rel_id}]->(c)
SET r.timestamp = $timestamp,
    r.context = $context,
    r.user_id = $user_id,
    r.created_at = $timestamp
RETURN r
"""

# Conversation-coarse: links every chunk in the conversation to every entity
# extracted from it. Enables BFS expansion in chronicle's search_memories.
LINK_CHUNKS_TO_ENTITIES = """
UNWIND $entity_ids AS eid
MATCH (e:Entity {id: eid})
MATCH (c:ConvChunk {conversation_id: $conversation_id})
MERGE (c)-[:MENTIONS]->(e)
"""

GET_ENTITIES_FROM_CONVERSATION = """
MATCH (e:Entity)-[:MENTIONED_IN]->(c:Conversation {conversation_id: $conversation_id})
RETURN e
ORDER BY e.name
"""

# =============================================================================
# GRAPH QUERIES
# =============================================================================

GET_ENTITY_GRAPH = """
MATCH (center:Entity {id: $entity_id})
OPTIONAL MATCH path = (center)-[r*1..2]-(connected:Entity)
WITH center, collect(DISTINCT connected) as connected_nodes,
     collect(DISTINCT relationships(path)) as rels
RETURN center, connected_nodes, rels
"""

GET_USER_GRAPH = """
MATCH (e:Entity)
OPTIONAL MATCH (e)-[r]->(e2:Entity)
WITH collect(DISTINCT e) as nodes, collect(DISTINCT {source: startNode(r).id, target: endNode(r).id, type: type(r), id: r.id, context: r.context, user_id: r.user_id}) as edges
RETURN nodes, edges
LIMIT $limit
"""

# =============================================================================
# CONVERSATION DOC QUERIES (ConvDoc / ConvEntity nodes from chronicle memory)
# =============================================================================

GET_CONVERSATION_DOCS = """
MATCH (d:ConvDoc)
OPTIONAL MATCH (d)-[:MENTIONS]->(e:ConvEntity)
RETURN d.conversation_id AS conversation_id,
       d.title AS title,
       d.summary AS summary,
       d.date AS date,
       d.updated_at AS updated_at,
       collect(CASE WHEN e IS NOT NULL
               THEN {name: e.name, description: e.description}
               ELSE NULL END) AS people
ORDER BY d.date DESC
LIMIT $limit
"""

GET_CONVERSATION_DOCS_BY_PERSON = """
MATCH (d:ConvDoc)-[:MENTIONS]->(e:ConvEntity)
WHERE toLower(e.name) CONTAINS toLower($person)
WITH d, collect({name: e.name, description: e.description}) AS matched_people
OPTIONAL MATCH (d)-[:MENTIONS]->(e2:ConvEntity)
RETURN d.conversation_id AS conversation_id,
       d.title AS title,
       d.summary AS summary,
       d.date AS date,
       d.updated_at AS updated_at,
       collect(CASE WHEN e2 IS NOT NULL
               THEN {name: e2.name, description: e2.description}
               ELSE NULL END) AS people
ORDER BY d.date DESC
LIMIT $limit
"""

GET_PEOPLE = """
MATCH (e:ConvEntity)<-[:MENTIONS]-(d:ConvDoc)
RETURN e.name AS name, e.description AS description, count(d) AS mention_count
ORDER BY mention_count DESC
"""

# =============================================================================
# CLEANUP QUERIES
# =============================================================================

# Drops every :Entity node (which includes the dual-label :Conversation:Entity
# nodes) in the per-user graph. Used as a partial wipe; full per-user wipe
# happens via GraphClient.delete_graph() in chronicle.delete_all_user_memories.
DELETE_USER_ENTITIES = """
MATCH (e:Entity)
DETACH DELETE e
RETURN count(e) as deleted_count
"""

DELETE_CONVERSATION_ENTITIES = """
MATCH (e:Entity)-[:MENTIONED_IN]->(c:Conversation {conversation_id: $conversation_id})
DETACH DELETE e
WITH count(e) as entity_count
MATCH (c:Conversation {conversation_id: $conversation_id})
DETACH DELETE c
RETURN entity_count + count(c) as deleted_count
"""
