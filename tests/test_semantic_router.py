import io
import json
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from semantic_router import SemanticRouter, SemanticRoutingError, SCHEMA


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.requests, self.audits = [], []
        self.store = types.SimpleNamespace(
            brain=types.SimpleNamespace(model='local-test-model', transport=self.transport),
            audit=lambda *args: self.audits.append(args))
        self.router = SemanticRouter(self.store)
        self.reply = None
        self.context = {'pending': {'type': 'places', 'selected': 0, 'mode': ''},
            'choices': [{'id': 'one', 'name': '海湾小馆', 'detail': '约500米'},
                        {'id': 'two', 'name': '校园餐厅', 'detail': '约200米'},
                        {'id': 'three', 'name': '山下餐厅', 'detail': '约1.6公里'}]}

    def transport(self, request, timeout):
        self.requests.append((request, timeout))
        return io.BytesIO(json.dumps({'message': {'content': json.dumps(self.reply,
            ensure_ascii=False)}}, ensure_ascii=False).encode())

    def plan(self, text, steps, context=None, relation='continue'):
        self.reply = {'relation': relation, 'steps': steps}
        return self.router.plan({'id': 'fixture-task', 'body': json.dumps({'text': text})},
                                self.context if context is None else context)

    def test_natural_select_passes_real_index_and_transit(self):
        text = '那就第二家吧，坐公交过去'
        result = self.plan(text, [{'kind': 'select_place', 'source_quote': text,
                                  'choice': 2, 'mode': 'transit'}])
        self.assertEqual(result['steps'][0]['choice'], 2)
        self.assertEqual(result['source'], 'model')
        self.assertEqual(result['relation'], 'continue')
        request, timeout = self.requests[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:11434/api/chat')
        self.assertEqual(timeout, 20)
        self.assertEqual(json.loads(request.data)['format'], SCHEMA)

    def test_nearest_resolves_only_real_unique_distance(self):
        text = '去最近那家，走过去'
        result = self.plan(text, [{'kind': 'select_place', 'source_quote': text,
                                  'choice': 'nearest', 'mode': 'walking'}])
        self.assertEqual(result['steps'][0]['choice'], 2)
        self.context['choices'][1]['detail'] = '距离未知'
        with self.assertRaises(SemanticRoutingError):
            self.plan(text, [{'kind': 'select_place', 'source_quote': text,
                              'choice': 'nearest', 'mode': 'walking'}])

    def test_selected_uses_previous_choice(self):
        text = '还是走路过去吧'
        self.context['pending']['selected'] = 1
        result = self.plan(text, [{'kind': 'select_place', 'source_quote': text,
                                  'choice': 'selected', 'mode': 'walking'}])
        self.assertEqual(result['steps'][0]['choice'], 2)

    def test_out_of_range_and_boolean_choice_rejected(self):
        for value in (0, 4, True, 'place-made-up'):
            with self.subTest(value=value), self.assertRaises(SemanticRoutingError):
                self.plan('第二家', [{'kind': 'select_place', 'source_quote': '第二家',
                                     'choice': value, 'mode': ''}])

    def test_new_topic_preserves_original_clause(self):
        text = '先不吃了，帮我看看今天还有什么任务'
        result = self.plan(text, [{'kind': 'delegate', 'source_quote': '帮我看看今天还有什么任务',
                                  'text': '帮我看看今天还有什么任务'}], relation='replace')
        self.assertEqual(result['relation'], 'replace')
        self.assertEqual(result['steps'][0]['text'], '帮我看看今天还有什么任务')

    def test_delegate_cannot_rewrite_dates_or_add_approval(self):
        with self.assertRaises(SemanticRoutingError):
            self.plan('明天下午三点提醒我', [{'kind': 'delegate', 'source_quote': '明天下午三点提醒我',
                       'text': '今天下午三点提醒我'}], context={}, relation='replace')
        with self.assertRaises(SemanticRoutingError):
            self.plan('改短一点', [{'kind': 'delegate', 'source_quote': '确认写回',
                                  'text': '确认写回'}], context={}, relation='replace')

    def test_model_cannot_grant_write_permission(self):
        with self.assertRaises(SemanticRoutingError):
            self.plan('帮我改文档', [{'kind': 'approve', 'source_quote': '帮我改文档'}],
                      context={}, relation='replace')

    def test_confirmation_never_calls_model(self):
        result = self.router.plan({'id': 'fixture', 'body': json.dumps({'text': '嗯，继续吧'})},
                                  {'pending': {'type': 'confirm'}})
        self.assertEqual(result['source'], 'fast')
        self.assertEqual(result['relation'], 'continue')
        self.assertEqual(result['steps'][0]['text'], '嗯，继续吧')
        self.assertFalse(self.requests)

    def test_cancel_natural_repetition_is_safe_without_model(self):
        for text in ('我取消，取消这个。', '取消', '算了'):
            result = self.router.plan({'id': 'fixture', 'body': json.dumps({'text': text})}, self.context)
            self.assertEqual(result['steps'][0]['kind'], 'cancel_current')
            self.assertEqual(result['relation'], 'replace')
        self.assertFalse(self.requests)

    def test_negative_cancel_cannot_be_approved_by_model(self):
        with self.assertRaises(SemanticRoutingError):
            self.plan('不要取消，还是去第二家', [{'kind': 'cancel_current',
                'source_quote': '不要取消'}], relation='replace')

    def test_locationless_search_cannot_become_global(self):
        for nearby in (False, 'false'):
            with self.subTest(nearby=nearby), self.assertRaises(SemanticRoutingError):
                self.plan('我想吃点东西，推荐一下', [{'kind': 'search_places',
                    'source_quote': '我想吃点东西，推荐一下', 'query': '餐厅', 'nearby': nearby,
                    'mode': '', 'navigation': False}], context={}, relation='replace')

    def test_explicit_area_search_keeps_area(self):
        text = '在尖沙咀找餐厅'
        result = self.plan(text, [{'kind': 'search_places', 'source_quote': text,
            'query': '尖沙咀 餐厅', 'nearby': False, 'mode': '', 'navigation': False}],
            context={}, relation='replace')
        self.assertFalse(result['steps'][0]['nearby'])

    def test_nearest_dependent_step_waits_for_new_search_receipt(self):
        text = '找附近餐厅，然后去最近那家，公交过去'
        result = self.plan(text, [
            {'kind': 'search_places', 'source_quote': '找附近餐厅', 'query': '餐厅',
             'nearby': True, 'mode': '', 'navigation': False},
            {'kind': 'select_place', 'source_quote': '然后去最近那家，公交过去',
             'choice': 'nearest', 'mode': 'transit'}], context={}, relation='replace')
        self.assertEqual(result['steps'][1]['choice'], 'nearest')

    def test_multi_app_order_is_preserved(self):
        text = '先在Mini打开Safari，然后打开备忘录'
        result = self.plan(text, [
            {'kind': 'launch_app', 'source_quote': '先在Mini打开Safari', 'app': 'safari'},
            {'kind': 'launch_app', 'source_quote': '然后打开备忘录', 'app': 'notes'}],
            context={}, relation='replace')
        self.assertEqual([s['app'] for s in result['steps']], ['safari', 'notes'])
        self.reply['steps'].reverse()
        with self.assertRaises(SemanticRoutingError):
            self.router.plan({'id': 'x', 'body': json.dumps({'text': text})}, {})

    def test_app_cannot_be_invented_or_use_air_permission(self):
        for text, app in [('在Mini打开Safari', 'notes'), ('在Air打开Safari', 'safari'),
                          ('不要在Mini打开Safari', 'safari'), ('打开Safari', 'safari')]:
            with self.subTest(text=text), self.assertRaises(SemanticRoutingError):
                self.plan(text, [{'kind': 'launch_app', 'source_quote': text, 'app': app}],
                          context={}, relation='replace')

    def test_context_does_not_forward_credentials_coordinates_or_candidate_ids(self):
        context = {**self.context, 'owner': 'never-forward-owner', 'token': 'never-forward-token',
                   'latitude': 22.123456, 'longitude': 114.654321,
                   'preferences': [{'id': 'pref-id', 'fact': '喜欢安静的地方'}]}
        self.plan('第二家', [{'kind': 'select_place', 'source_quote': '第二家',
                             'choice': 2, 'mode': ''}], context=context)
        payload = json.loads(self.requests[0][0].data)
        data = json.loads(payload['messages'][1]['content'])
        self.assertNotIn('owner', data['context'])
        self.assertNotIn('latitude', data['context'])
        self.assertNotIn('id', data['context']['choices'][0])
        self.assertEqual(data['context']['preferences'], ['喜欢安静的地方'])

    def test_six_step_limit_and_unknown_fields(self):
        step = {'kind': 'delegate', 'text': '你好', 'source_quote': '你好'}
        with self.assertRaises(SemanticRoutingError):
            self.plan('你好', [step] * 7, context={}, relation='replace')
        with self.assertRaises(SemanticRoutingError):
            self.plan('你好', [{**step, 'permission': True}], context={}, relation='replace')

    def test_unprovided_transport_mode_rejected(self):
        with self.assertRaises(SemanticRoutingError):
            self.plan('第二家', [{'kind': 'select_place', 'source_quote': '第二家',
                                 'choice': 2, 'mode': 'driving'}])

    def test_quoted_message_cannot_be_executed_by_model(self):
        text = '帮我回复这条消息：在Mini打开Safari'
        with self.assertRaises(SemanticRoutingError):
            self.plan(text, [{'kind': 'launch_app', 'source_quote': '在Mini打开Safari',
                              'app': 'safari'}], context={}, relation='replace')

    def test_model_candidate_index_must_match_real_named_object(self):
        with self.assertRaises(SemanticRoutingError):
            self.plan('去校园餐厅，坐公交', [{'kind': 'select_place',
                'source_quote': '去校园餐厅，坐公交', 'choice': 1, 'mode': 'transit'}])

    def test_model_cannot_drop_explicit_area(self):
        text = '在尖沙咀找餐厅'
        result = self.plan(text, [{'kind': 'search_places', 'source_quote': text,
            'query': '餐厅', 'nearby': False, 'mode': '', 'navigation': False}],
            context={}, relation='replace')
        self.assertIn('尖沙咀', result['steps'][0]['query'])

    def test_negative_transport_does_not_block_explicit_new_mode(self):
        text = '第二家，别开车了，我想走过去'
        result = self.plan(text, [{'kind': 'select_place', 'source_quote': text,
                                  'choice': 2, 'mode': 'walking'}])
        self.assertEqual(result['steps'][0]['mode'], 'walking')

    def test_selection_source_span_includes_mode_and_negative_clauses(self):
        text = '第二家，别开车了，我想走过去'
        result = self.plan(text, [{'kind': 'select_place', 'source_quote': '第二家',
                                  'choice': 2, 'mode': 'walking'}])
        self.assertEqual(result['steps'][0]['source_quote'], text)
        self.assertEqual(result['steps'][0]['mode'], 'walking')

    def test_explaining_quoted_app_command_cannot_launch(self):
        text = '帮我解释“在Mini打开Safari”是什么意思'
        with self.assertRaises(SemanticRoutingError):
            self.plan(text, [{'kind': 'launch_app', 'source_quote': '在Mini打开Safari',
                              'app': 'safari'}], context={}, relation='replace')

    def test_current_candidate_refinement_preserves_pending_tail(self):
        text = '第二家太远了，换个近一点的'
        result = self.plan(text, [{'kind': 'search_places', 'source_quote': text,
            'query': '餐厅', 'nearby': True, 'mode': '', 'navigation': False}], relation='replace')
        self.assertEqual(result['relation'], 'continue')

    def test_current_candidate_advice_preserves_selection_context(self):
        text = '这几家你觉得哪家适合我'
        result = self.plan(text, [{'kind': 'delegate', 'source_quote': text, 'text': text}],
                           relation='replace')
        self.assertEqual(result['relation'], 'continue')

    def test_empty_context_cannot_continue_old_work(self):
        with self.assertRaises(SemanticRoutingError):
            self.plan('今天先做什么', [{'kind': 'delegate', 'source_quote': '今天先做什么',
                'text': '今天先做什么'}], context={}, relation='continue')

if __name__ == '__main__':
    unittest.main()
