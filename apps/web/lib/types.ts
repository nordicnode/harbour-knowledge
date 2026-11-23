export type ConfidenceBand = "high" | "medium" | "low";
export type AnswerStatus = "draft" | "in_review" | "approved" | "needs_recheck" | "archived";
export type Visibility = "internal" | "public";

export interface Meta {
  workspace: { slug: string; name: string };
  member: { name: string; email: string; role: "admin" | "editor" | "agent"; title: string };
  ai: {
    mode: "dev" | "openai";
    provider: string;
    embedding_model: string;
    embedding_provider: "dev" | "openai" | "local";
    embedding_version: string;
    embedding_dim: number;
    embedding_identity: string;
    index: {
      compatible_passages: number;
      stale_passages: number;
      total_passages: number;
      needs_reindex: boolean;
    };
  };
  thresholds: {
    internal: { low: number; high: number };
    public: { low: number; high: number };
  };
  review?: { pending: number };
  channels: string[];
}

export interface Citation {
  evidence_id: string;
  passage_id: string;
  quote: string;
  source_title: string;
  source_date: string | null;
  section_path: string;
  verified: boolean;
}

export interface AskResult {
  question: string;
  standalone_query: string;
  channel: string;
  mode: "canonical" | "fallback" | "abstained";
  answer_md: string | null;
  citations: Citation[];
  confidence_band: ConfidenceBand;
  confidence: {
    composite: number;
    retrieval: number;
    faithfulness: number;
    historical: number;
  } | null;
  sources_ignored: { title: string; reason: string }[];
  conflict_notice: string | null;
  abstained: boolean;
  abstention_reason?: string;
  searched_scope: string;
  next_steps: string[];
  gap_id: string | null;
  answer_id: string | null;
  answer_version: number | null;
  qa_log_id: string;
  latency_ms: number;
  provider: string;
  retrieval_mode: "hybrid" | "lexical_only" | "exact";
  dense_scan: "exact" | "hnsw" | "none";
  index_warning: string | null;
}

export interface Topic {
  id: string;
  slug: string;
  name: string;
  description: string;
  parent_id: string | null;
  owner_name?: string | null;
  review_interval_days: number;
  approved_count?: number;
  answer_count?: number;
  source_count?: number;
}

export interface Answer {
  id: string;
  topic_id: string | null;
  topic_name?: string | null;
  topic_slug?: string | null;
  question: string;
  answer_md: string;
  status: AnswerStatus;
  owner_id: string | null;
  owner_name?: string | null;
  review_interval_days: number;
  next_review_at: string | null;
  plan_scope: string[];
  version_scope: string[];
  visibility: Visibility;
  human_confidence: number | null;
  current_version: number;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
  evidence_count?: number;
  stale_evidence?: number;
  overdue?: boolean;
}

export interface AnswerEvidence {
  id: string;
  passage_id: string;
  quote: string;
  claim_ref: string | null;
  text: string;
  section_path: string;
  status: string;
  document_id: string;
  document_title: string;
  effective_from: string | null;
  source_id: string;
  source_title: string;
  source_visibility: Visibility;
  quote_verified: boolean;
}

export interface AnswerVersion {
  version: number;
  answer_md: string;
  change_reason: string | null;
  created_at: string;
  changed_by_name?: string | null;
}

export interface AnswerDetail {
  answer: Answer;
  evidence: AnswerEvidence[];
  versions: AnswerVersion[];
  review_items: ReviewItem[];
}

export interface ReviewItem {
  id: string;
  kind: string;
  status: string;
  priority: number;
  payload: Record<string, any>;
  created_at: string;
  summary?: string;
  conflict?: {
    id: string;
    score: number | null;
    status: string;
    quote_a: string | null;
    quote_b: string | null;
    document_a: string | null;
    document_b: string | null;
    effective_from_a: string | null;
    effective_from_b: string | null;
    source_a: string | null;
    source_b: string | null;
    visibility_a: string | null;
    visibility_b: string | null;
    source_tier_a: string | null;
    source_tier_b: string | null;
  } | null;
  answer?: Answer | null;
  suggested_md?: string | null;
  suggested_evidence?: { id: string; text: string; section_path: string; document_title: string; source_title: string }[];
}

export interface Gap {
  id: string;
  normalised_question: string;
  representative_question: string;
  count: number;
  first_seen_at: string;
  last_seen_at: string;
  channels: string[];
  status: "open" | "answered" | "wont_answer";
  linked_answer_id: string | null;
  linked_question?: string | null;
}

export interface SourceRow {
  id: string;
  kind: string;
  title: string;
  origin_url: string | null;
  visibility: Visibility;
  source_tier: string;
  status: string;
  hash_sha256: string;
  alias_of_id: string | null;
  plan_scope: string[];
  product_version_scope: string[];
  created_at: string;
  ingested_at: string | null;
  document_id: string | null;
  document_version: number | null;
  doc_type: string | null;
  topic_id: string | null;
  topic_name: string | null;
  passage_count: number | null;
  job_status: string | null;
  job_stage: string | null;
  job_error: string | null;
  alias_count: number;
}

export interface SourcePassage {
  id: string;
  level: "section" | "chunk";
  text: string;
  section_path: string;
  page_ref: string | null;
  chunk_index: number;
  status: string;
  token_count: number;
  contextual_blurb: string;
}

export interface SourceDetail {
  source: SourceRow & { metadata: Record<string, unknown>; size_bytes: number | null; mime_type: string | null };
  document: (Record<string, any> & { content_md: string; title: string; doc_type: string; topic_name?: string | null }) | null;
  content_truncated: boolean;
  passages: SourcePassage[];
  jobs: {
    id: string;
    status: string;
    stage: string | null;
    error: string | null;
    attempts: number;
    stats: Record<string, unknown>;
    created_at: string;
    finished_at: string | null;
  }[];
  alias_of: { id: string; title: string } | null;
}

export interface Overview {
  stats: {
    pending_review: number;
    pending_over_7d: number;
    conflicts_pending: number;
    open_gaps: number;
    gap_questions: number;
    approved_answers: number;
    in_review: number;
    answers_overdue: number;
    sources: number;
    failed_sources: number;
    passages: number;
    questions_7d: number;
    abstentions_7d: number;
    fallback_7d: number;
    abstention_rate: number;
    fallback_rate: number;
    approved_coverage: number;
    stale_pct: number;
  };
  needs_attention: ReviewItem[];
  gaps: Gap[];
  recent_questions: {
    question: string;
    channel: string;
    mode: string | null;
    abstained: boolean;
    confidence_band: string | null;
    created_at: string;
  }[];
}

export interface SearchResponse {
  query: string;
  results: {
    passage_id: string;
    parent_id: string | null;
    text: string;
    matched_text: string;
    section_path: string;
    document_id: string;
    source_id: string;
    document_title: string;
    source_title: string;
    source_tier: string;
    visibility: Visibility;
    topic_id: string | null;
    topic_name: string | null;
    document_version: number;
    score: number;
    coverage: number;
  }[];
  candidates_scanned: number;
  coverage: number;
  exact: boolean;
  retrieval_mode: "hybrid" | "lexical_only" | "exact";
  dense_scan: "exact" | "hnsw" | "none";
  index_warning: string | null;
}
