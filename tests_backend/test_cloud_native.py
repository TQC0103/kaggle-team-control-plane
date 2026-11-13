import copy
import hashlib
import unittest

try:
    import torch
except ImportError:
    raise unittest.SkipTest("Native checkpoint fixtures require CPU PyTorch")

from cloud_orchestrator.native_checkpoint import validate_state, tensor_schema, history_prefix


class NativeNegativeFixtures(unittest.TestCase):
    def setUp(self):
        def model(): return {'weight': torch.tensor([1.0])}
        h = dict(validation=[], val_h=[dict(iteration=200, mean_dice=.75,
                cases=[dict(case_id='a', dice=.7), dict(case_id='b', dice=.8)])],
                 and_arbitration=[{} for _ in range(4)], iteration_time=[.01]*200)
        frozen = hashlib.sha256(b'weight'+model()['weight'].numpy().tobytes()).hexdigest()
        self.s = dict(iter_num=200, immutable=dict(max_iterations=15000, seed=1337, FM_ARM='DF65'),
            teacher_math=model(), teacher_ling=model(), student=model(),
            t_optimizer={}, s_optimizer={}, t_scheduler=dict(last_epoch=200), s_scheduler=dict(last_epoch=200),
            fm_state=dict(initial_frozen_SHA=frozen), history=h, python_rng=('fixture',), numpy_rng=('fixture',),
            torch_rng=torch.zeros(10, dtype=torch.uint8), cuda_rng=[torch.zeros(10, dtype=torch.uint8)],
            dose_state=dict(generator_seed=20261001, k=0), best_student_state=model(), best_teacher_state=model(),
            best_dice_s=.75, best_iter_s=200)
        self.c = dict(schema=1, seed=1337, config=dict(arm='DF65', seed=1337, stop=200, val_h_ids=['a', 'b']),
            expected_cuda_rng_devices=1, model_schemas={k: tensor_schema(self.s[k]) for k in ['teacher_math','teacher_ling','student']},
            frozen_reference=dict(parameter_order=['weight'], initial_sha256=frozen),
            parent_history_prefix=history_prefix(dict(iteration_time=[.01]*100)))

    def check(self, **overrides):
        return validate_state(self.s, overrides.get('history', self.s['history']),
            overrides.get('best_student', copy.deepcopy(self.s['best_student_state'])),
            copy.deepcopy(self.s['best_teacher_state']), self.c)

    def test_valid_full_state_passes_without_modification(self):
        before = self.s['student']['weight'].clone()
        self.assertEqual(self.check()['endpoint_Dice'], .75)
        self.assertTrue(torch.equal(before, self.s['student']['weight']))

    def test_missing_rng_is_rejected(self):
        del self.s['cuda_rng']
        with self.assertRaises(AssertionError): self.check()

    def test_nonfinite_tensor_is_rejected(self):
        self.s['teacher_math']['weight'].fill_(float('nan'))
        with self.assertRaises(AssertionError): self.check()

    def test_frozen_weight_drift_is_rejected(self):
        self.s['student']['weight'].add_(1)
        with self.assertRaises(AssertionError): self.check()

    def test_buffer_or_parameter_shape_drift_is_rejected(self):
        self.s['teacher_ling']['unexpected_buffer'] = torch.ones(1)
        with self.assertRaises(AssertionError): self.check()

    def test_wrong_scheduler_is_rejected(self):
        self.s['s_scheduler']['last_epoch'] = 199
        with self.assertRaises(AssertionError): self.check()

    def test_history_prefix_drift_is_rejected(self):
        self.s['history']['iteration_time'][0] = .02
        with self.assertRaises(AssertionError): self.check()

    def test_wrong_seed_is_rejected(self):
        self.s['immutable']['seed'] = 2027
        with self.assertRaises(AssertionError): self.check()

    def test_validation_case_swap_is_rejected(self):
        self.s['history']['val_h'][0]['cases'][0]['case_id'] = 'test-case'
        with self.assertRaises(AssertionError): self.check()

    def test_unexpected_evaluation_history_is_rejected(self):
        self.s['history']['validation'].append({'unexpected': True})
        with self.assertRaises(AssertionError): self.check()

    def test_best_weight_mismatch_is_rejected(self):
        with self.assertRaises(AssertionError): self.check(best_student={'weight': torch.tensor([99.])})

    def test_inconsistent_patient_mean_is_rejected(self):
        self.s['history']['val_h'][0]['mean_dice'] = .8
        with self.assertRaises(AssertionError): self.check()


if __name__ == '__main__':
    unittest.main()
