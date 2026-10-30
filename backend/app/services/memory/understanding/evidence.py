"""Conservative local support gate, not a general-purpose semantic oracle."""
import re
import unicodedata
from dataclasses import dataclass

from app.services.memory.understanding.schemas import EpisodicCandidate
from app.services.dialogue import origin_of, confirmation_pair


def normalize(value):
    return re.sub(r'[\s，。！？、；：“”‘’"\'.,!?;:]+','',unicodedata.normalize('NFKC',value)).casefold()


@dataclass(frozen=True)
class EvidenceDecision:
    disposition: str  # active / tentative / reject
    reason: str


class EvidenceValidator:
    def structural(self,spans,messages):
        by_id={m.id:m for m in messages}
        for span in spans:
            m=by_id.get(span.message_id)
            if m is None:return 'source_outside_scope'
            if m.role!=span.role:return 'role_mismatch'
            if span.end<=span.start or m.content[span.start:span.end]!=span.quote or span.end>len(m.content):return 'span_mismatch'
        return None

    def validate(self,candidate,messages):
        error=self.structural(candidate.evidence,messages)
        if error:return EvidenceDecision('reject',error)
        by_id={m.id:m for m in messages}
        if candidate.evidence and all(by_id[e.message_id].metadata.get('import_contract')=='deeptalk-text-reviewed-v1' for e in candidate.evidence):
            # Trusted import receipts admit only the exact human-reviewed batch;
            # the ordinary forwarded-material path below remains unchanged.
            from app.services.memory.history_import import ReviewedHistoryEvidence
            return ReviewedHistoryEvidence().validate(candidate,messages)
        # Inspect WHOLE original messages, so quoting a safe substring cannot
        # launder a hypothetical/third-party assertion into a user fact.
        originals=[by_id[e.message_id].content for e in candidate.evidence]
        user=[by_id[e.message_id].content for e in candidate.evidence if e.role=='user']
        user_quotes=[e.quote for e in candidate.evidence if e.role=='user']
        all_text='\n'.join(originals)
        if re.search(r'忽略.{0,8}(指令|规则)|系统提示|system\s*prompt|写入.{0,8}(active|事实)|标记为已确认',all_text,re.I):
            return EvidenceDecision('reject','instruction_content')
        for value in (candidate.valid_from,candidate.valid_to):
            # Explicit UTC literal only. Relative dates are not silently resolved.
            if value and not any(value in e.quote or value.replace('.000000Z','Z') in e.quote for e in candidate.evidence):
                return EvidenceDecision('reject','unsupported_time')
        episode=isinstance(candidate,EpisodicCandidate)
        if any(origin_of(by_id[e.message_id]).kind=='user_forwarded_material' for e in candidate.evidence if e.role=='user'):
            # K1 archives forwarded material; formatter/admission is deferred.
            # Neither semantic claims nor Episode.user_view may launder its role.
            return EvidenceDecision('reject','forwarded_not_user_fact')
        if episode:
            for point in candidate.discussion:
                if self.structural(point.evidence,messages):return EvidenceDecision('reject','discussion_source')
                # Discussion evidence cannot expand the task's candidate scope.
                if set(e.message_id for e in point.evidence)-set(e.message_id for e in candidate.evidence):return EvidenceDecision('reject','discussion_scope')
                if not any(normalize(point.content)==normalize(e.quote) for e in point.evidence):return EvidenceDecision('reject','discussion_paraphrase')
                if point.perspective=='user_view' and not any(e.role=='user' and normalize(point.content)==normalize(e.quote) for e in point.evidence):return EvidenceDecision('reject','discussion_role')
                if point.perspective=='assistant_interpretation' and not any(e.role=='assistant' and normalize(point.content)==normalize(e.quote) for e in point.evidence):return EvidenceDecision('reject','discussion_role')
                if point.perspective=='agreed_conclusion' and not self.confirmed(point.content,candidate,messages):return EvidenceDecision('reject','unconfirmed_conclusion')
            for followup in (candidate.continuation,candidate.continuation_condition):
                if followup and not any(normalize(followup) in normalize(e.quote) for e in candidate.evidence):return EvidenceDecision('reject','unsupported_continuation')
            supported=any(normalize(candidate.content)==normalize(e.quote) for e in candidate.evidence)
            if not supported:return EvidenceDecision('tentative','summary_needs_review')
            # Store the fact that an event/utterance occurred, never a personality.
            if not user or candidate.evidence_level=='model_inferred' or candidate.uncertainty!='none':return EvidenceDecision('tentative','uncertain_episode')
            return EvidenceDecision('active','quoted_episode')
        if not user:return EvidenceDecision('reject','assistant_not_user_fact')
        if any(re.search(r'[?？]|是不是|会不会|难道|假如|假设|如果|要是|比如说|举个例子|想象|梦见|小说|扮演|不要记|朋友说|他说|她说|老师说|有人说|[“”「」『』"]',s) for s in user):
            return EvidenceDecision('reject','nonassertive_or_quoted')
        if re.search(r'人格|依恋型|讨好型|回避型|你是.{0,8}的人|我天生|我永远|内向|外向|心理障碍',candidate.content):
            return EvidenceDecision('tentative','personality_requires_review')
        if re.search(r'今天|刚才|这会儿|烦死|累死|睡着|生气|难过|崩溃|好累|暂时',candidate.content):
            return EvidenceDecision('reject','transient_not_semantic')
        if candidate.evidence_level=='model_inferred' or candidate.uncertainty!='none':return EvidenceDecision('tentative','unconfirmed_inference')
        if candidate.confidence<.8:return EvidenceDecision('tentative','low_confidence')
        if any(re.search(r'可能|好像|也许|不确定|似乎|大概|我猜|我怀疑',s) for s in user):return EvidenceDecision('tentative','uncertain_original')
        if candidate.evidence_level=='user_confirmed':
            return EvidenceDecision('active','explicit_agreement') if self.confirmed(candidate.content,candidate,messages) else EvidenceDecision('reject','missing_explicit_agreement')
        # An extractive first-person proposition is a deliberately narrow
        # verified subset. Free paraphrases remain uncertain even at confidence 1.
        value=normalize(candidate.content)
        clauses=[part for q in user for part in re.split(r'[，。；\n]|但是|不过',q) if part.strip()]
        # A contained string may be negated or refer to a previous belief.
        # Verify a whole assertion clause, not an arbitrary convenient substring.
        direct=any(value in normalize(q) for q in user_quotes) and (any(normalize(s)==value for s in user) or any(normalize(clause)==value or normalize(clause) in ('现在'+value,'现在已经'+value) for clause in clauses))
        historical=bool(re.search(r'我(以前|曾经|过去|原来)|以前我|过去我|原来我',candidate.content))
        if value.startswith('我') and direct and not historical:
            return EvidenceDecision('active','direct_user_proposition')
        return EvidenceDecision('tentative','semantic_support_uncertain')

    def confirmed(self,content,candidate,messages):
        by_id={m.id:m for m in messages}
        matched=[by_id[e.message_id] for e in candidate.evidence if e.role=='assistant' and normalize(content)==normalize(e.quote)==normalize(by_id[e.message_id].content)]
        for original in matched:
            for e in candidate.evidence:
                if e.role!='user':continue
                user=by_id[e.message_id]
                # Explicit, complete acknowledgement immediately following the
                # referenced interpretation; no inferred silence or generic 嗯.
                if confirmation_pair(original,user,messages):return True
        return False
