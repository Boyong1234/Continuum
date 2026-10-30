"""K1 pure local policies. No IO, model, persistent state or extraction scheduler."""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

KERNEL_VERSION = 'k1.8'
CONSTITUTION_VERSION = '1.3'


class Depth(StrEnum):
    LIGHT = 'LIGHT'
    NORMAL = 'NORMAL'
    DEEP = 'DEEP'


class Initiative(StrEnum):
    LOW = 'LOW'
    NORMAL = 'NORMAL'
    HIGH = 'HIGH'


class Recall(StrEnum):
    NONE = 'NONE'
    LOCAL = 'LOCAL'


class ResponseLength(StrEnum):
    SHORT = 'SHORT'
    NORMAL = 'NORMAL'
    LONG = 'LONG'


@dataclass(frozen=True)
class TurnPlan:
    depth: Depth = Depth.NORMAL
    initiative: Initiative = Initiative.NORMAL
    recall: Recall = Recall.NONE
    response_length: ResponseLength = ResponseLength.NORMAL

    def instruction(self):
        parts=[]
        if self.response_length == ResponseLength.LONG:parts.append('按用户要求充分展开')
        if self.initiative == Initiative.HIGH:parts.append('给出有依据的方向；若正在冲动执行不可逆操作，先建议暂缓，不替用户执行')
        return '\n本轮表达建议：'+'；'.join(parts)+'。' if parts else ''



@dataclass(frozen=True)
class OriginView:
    kind: str
    assertable: bool = False


def metadata_of(message):
    value = getattr(message, 'metadata_json', None)
    if not isinstance(value, dict):
        value = getattr(message, 'metadata', None)
    return value if isinstance(value, dict) else {}


def origin_of(message):
    if message.role != 'user':
        return OriginView('assistant_interpretation')
    value, meta = message.content, metadata_of(message)
    if meta.get('origin') in ('forwarded', 'historical_import', 'imported') or meta.get('source_kind') in ('forwarded', 'historical_import') or meta.get('source_system') in ('chatgpt', 'gpt'):
        return OriginView('user_forwarded_material')
    if re.search(r'(GPT|ChatGPT|AI).{0,16}(总结|回复|分析)|朋友说|他说|她说|老师说|他人消息|转发|摘录|文档片段|引用|[「」『』“”\'"]', value, re.I):
        return OriginView('user_forwarded_material')
    if re.search(r'[?？]|是不是|会不会|难道|吗[。！ ]*$|假设|假如|如果|要是|比如说|我猜|也许|可能|认可', value):
        return OriginView('uncertain')
    return OriginView('direct_user_statement', True)


def continues_turn(content):
    return bool(re.fullmatch(r'(继续|接着说|继续聊这个)[。！! ]*',content))


def requests_history(content):
    """Conversation reference, not general past tense. Deliberately narrow."""
    return bool(
        re.search(r'还记得|历史比较|(?:我们|咱们)(?:之前|以前|上次|前几天)?聊', content)
        or re.search(r'(?:我|我们|咱们).{0,8}(?:跟你|和你|向你).{0,6}(?:说|提|聊|谈|问)', content)
        or re.search(r'(?:之前|以前|上次|前几天).{0,8}问过你|(?:之前|以前|上次)(?:的)?那个', content)
        or re.search(r'(?:我们|咱们)(?:最后|当时).{0,6}(?:决定|定下).{0,6}(?:来着|什么|怎么)|当时.{0,8}(?:没解决|未解决)',content)
    )


def stable_communication_preference(content):
    row=type('Input',(),{'role':'user','content':content})()
    return (origin_of(row).assertable and content.startswith('我')
            and bool(re.search(r'聊天|交流|回复|回答|说话|建议|分析|追问|列计划',content))
            and bool(re.search(r'长期|通常|平时|以后|今后|一直',content))
            and bool(re.search(r'我(?:长期)?(?:希望|偏好|更喜欢|不喜欢|不需要)',content)))


def communication_feedback_turn(content):
    row=type('Input',(),{'role':'user','content':content})()
    if re.search(r'认真分析|详细分析|完整方案|帮我决定',content):return False
    return (origin_of(row).assertable and bool(re.search(
        r'我(?:只是)?想(?:找你|和你)?聊|先听我说|你.{0,6}(?:说话|说得|回复|回答).{0,8}(?:生硬|机械)|你不用每次|不用每次.{0,6}(?:建议|计划|分析|追问)',content)))


def needs_irreversible_pause(content):
    """Explicit first-person impulse + a bounded destructive/exit action."""
    if re.search(r'如果|假如|假设|不想|不打算|不要|不会', content):
        return False
    for clause in re.split(r'[。！？?\n]', content):
        if not ('我' in clause and re.search(r'想|要|打算|准备', clause) and re.search(r'突然|烦|气|受不了|冲动|算了', clause)):
            continue
        destructive = re.search(r'删|清空', clause) and re.search(r'整个项目|重要数据|全部数据', clause)
        exit_now = re.search(r'马上|立刻|立即', clause) and re.search(r'退出|终止', clause) and '合作' in clause
        if destructive or exit_now:
            return True
    return False


def plan_turn(content, previous_depth=None):
    # Explicit routing only; no user psychology, large keyword dictionary or LLM.
    row = type('Input', (), {'role': 'user', 'content': content})()
    if origin_of(row).kind == 'user_forwarded_material':
        return TurnPlan()
    analysis = bool(re.search(r'认真分析|深入分析|详细分析|展开讨论', content))
    deep = len(content) >= 600 or analysis or (previous_depth == Depth.DEEP and continues_turn(content))
    explicit_long = re.search(r'详细分析|全面分析|完整梳理|逐条.{0,4}分析|完整方案|一次讲清楚|一次讲完|写.{0,6}报告', content)
    light = len(content.strip()) <= 16 and not explicit_long and not re.search(r'分析|讨论|选择|决定|记得|之前|上次|以前|[?？]', content)
    depth = Depth.DEEP if deep else Depth.LIGHT if light else Depth.NORMAL
    high = needs_irreversible_pause(content) or re.search(r'帮我(?:做选择|决策|决定)|请帮我权衡|直接给.{0,4}(?:建议|方向)|建议我怎么做', content)
    initiative = Initiative.HIGH if high else Initiative.LOW if light or re.search(r'不要追问|先听我说|还没说完', content) else Initiative.NORMAL
    recall = Recall.LOCAL if requests_history(content) else Recall.NONE
    # Input length controls depth, never by itself the requested exposition.
    length = ResponseLength.LONG if explicit_long else ResponseLength.NORMAL if deep or high else ResponseLength.SHORT if light else ResponseLength.NORMAL
    if re.search(r'简短|简单说|一句话', content):
        length = ResponseLength.SHORT
    return TurnPlan(depth, initiative, recall, length)


def constitution_version(core):
    return CONSTITUTION_VERSION + '-' + hashlib.sha256(core.encode()).hexdigest()[:12]


def trace_seed(content, config=None, constitution='unavailable'):
    plan = plan_turn(content)
    return dict(kernel_version=KERNEL_VERSION, constitution_version=constitution,
                depth=plan.depth.value, initiative=plan.initiative.value, recall_mode=plan.recall.value,
                selected_memory_ids=[], recent_context_estimate=0, memory_context_estimate=0,
                provider=config.provider if config else 'unconfigured', model=config.model if config else None,
                usage={'source': 'unknown', 'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None}, status='running')


def parsed_time(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp if stamp.tzinfo is not None else None
    except (ValueError, AttributeError, TypeError):
        return None


def ingestion_time(message):
    meta = metadata_of(message)
    return parsed_time(meta.get('ingested_at') or meta.get('received_at'))


def live_source(message):
    meta = metadata_of(message)
    if meta.get('late') or meta.get('origin') in ('historical_import', 'imported') or meta.get('source_kind') == 'historical_import':
        return False
    event, ingested = parsed_time(getattr(message, 'created_at', None)), ingestion_time(message)
    if ingested is not None and event is None:
        return False
    return not (event and ingested and (ingested - event).total_seconds() > 300)


def confirmation_pair(original, user, messages):
    """Exact adjacent acknowledgement, event AND arrival order; never UUID tie-break."""
    if user.role != 'user' or not live_source(user) or not live_source(original):
        return False
    if getattr(original, 'conversation_id', None) != getattr(user, 'conversation_id', None):
        return False
    if not re.fullmatch(r'(这个总结我认可|(?:是的|对的|没错)[，, ]*(?:这个说法准确|这个结论准确)[，, ]*我(?:明确)?认可)[。.!！]?', user.content):
        return False
    a, b = parsed_time(original.created_at), parsed_time(user.created_at)
    if not a or not b or b <= a:
        return False
    times = [ingestion_time(m) for m in messages]
    if any(t is not None for t in times):
        # Partial arrival metadata cannot prove adjacency.
        if any(t is None for t in times):
            return False
    else:
        times = [parsed_time(m.created_at) for m in messages]
    if any(t is None for t in times) or len(set(times)) != len(times):
        return False
    ordered = [m for _, m in sorted(zip(times, messages), key=lambda x: x[0])]
    ids = [m.id for m in ordered]
    return original.id in ids and ids.index(original.id) + 1 < len(ids) and ids[ids.index(original.id)+1] == user.id


@dataclass(frozen=True)
class CurrentFact:
    topic: str
    state: str
    source_id: str


def normalized_topic(value):
    # Only the explicit exam regression alias, NOT a recall synonym dictionary.
    return value.replace('计算机网络', '计网')


def project_current(messages):
    """Tiny ephemeral completion projection, no general fact/personality extractor."""
    facts = {}
    sources = [source for row in messages for source in getattr(row, 'source_rows', (row,))]
    for row in sources:
        if not getattr(row, 'id', None):
            continue
        if not origin_of(row).assertable or not live_source(row):
            continue
        value = normalized_topic(row.content)
        if re.search(r'之前|以前|曾经', value):
            continue
        pending = re.search(r'([\w\u4e00-\u9fff]{1,30}?)(?:还没(?:有)?|尚未|没有|没)考完', value)
        match = pending or re.search(r'([\w\u4e00-\u9fff]{1,30}?)(?:终于|已经)?考完了', value)
        if match:
            topic = match[1].removeprefix('我').removesuffix('考试')
            facts[topic] = CurrentFact(topic, 'exam_pending' if pending else 'exam_completed', str(row.id))
    return tuple(list(facts.values())[-8:])


def filter_past(memories, facts, current):
    historical = bool(re.search(r'之前|以前|曾经|那时候', current.content)) and origin_of(current).kind != 'user_forwarded_material'
    result = []
    for memory in memories:
        value = normalized_topic(memory.get('content', ''))
        conflicts = any(f.topic in value and re.search(r'备考|准备.{0,20}考试' if f.state=='exam_completed' else r'考完|考试结束', value) for f in facts)
        if conflicts and not historical:
            continue
        item = dict(memory)
        item['temporal_role'] = 'historical_only' if historical else 'historical_reference_not_current_fact'
        result.append(item)
    return result
