"""B2 policy tightens tentative retention; B1's backup verifier stays stable."""
import re
from app.services.memory.understanding.evidence import EvidenceValidator,EvidenceDecision,normalize
from app.services.memory.understanding.schemas import EpisodicCandidate
from app.services.dialogue import stable_communication_preference


class EvidencePolicyV2(EvidenceValidator):
    def __init__(self,excluded=(),*,selective=False):self.excluded=set(excluded);self.selective=selective

    def validate(self,c,messages):
        if any(e.message_id in self.excluded for e in c.evidence):return EvidenceDecision('reject','source_excluded')
        base=super().validate(c,messages)
        if base.disposition=='reject':return base
        by_id={m.id:m for m in messages};user=[by_id[e.message_id].content for e in c.evidence if e.role=='user']
        # Conditional assent must not silently become an unconditional trait.
        if c.evidence_level=='user_confirmed' and any(re.search(r'只有|只在|仅在|但是|不过|不一定|暂时|这段时间',s) for s in user):
            return EvidenceDecision('reject','conditional_agreement_not_general_fact')
        if isinstance(c,EpisodicCandidate):
            if base.reason=='summary_needs_review':return EvidenceDecision('reject','unverified_event_rewrite')
            if len(normalize(c.content))<8 or re.fullmatch(r'(早上好|早呀|哈哈|嗯嗯|好的|晚安|收到|谢谢)[，。!！\s]*',c.content):
                return EvidenceDecision('reject','low_memory_value')
            return base
        value=normalize(c.content)
        if self.selective and not stable_communication_preference(c.content) and not re.search(r'长期|一直|通常|偏好|更喜欢|目标|计划|持续|负责|专业|住在|使用|项目|研究|毕业|打算',c.content):
            return EvidenceDecision('reject','not_stable_core_memory')
        original=any(normalize(s)==value for s in user) or any(normalize(sentence)==value for s in user for sentence in re.split(r'[。；!！?？\n]',s) if sentence.strip())
        if base.disposition=='tentative':
            # Preserve grounded uncertainty/self-reflection, not an invented
            # event, qualification, psychological label or unsupported rewrite.
            if not original:return EvidenceDecision('reject','unsupported_tentative')
            if re.search(r'人格|依恋型|讨好型|回避型|天生|永远|心理障碍',c.content) and c.uncertainty!='hypothesis':
                return EvidenceDecision('reject','unfounded_personality_generalization')
        durable=bool(re.search(r'长期|通常|偏好|更喜欢|不喜欢|计划|目标|希望|正在|目前|住在|专业|养了|负责|使用|研究|我认为|当时想表达|方向|以后|毕业|打算|是否|独立想',c.content))
        if not durable:return EvidenceDecision('reject','low_memory_value')
        # Reject literal hypothetical narrative too, even if it evades the small
        # B1 conditional dictionary; uncertainty can still be retained above.
        if re.search(r'倘若|设想|虚构|角色扮演|仅作举例|假想', ' '.join(user)):
            return EvidenceDecision('reject','nonassertive_original')
        return base
