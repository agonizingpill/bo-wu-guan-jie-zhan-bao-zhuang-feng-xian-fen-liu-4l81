import hashlib
import json

import numpy as np

SEED = 2611
THRESHOLD = 0.58
SMOOTHING = 12
TEST_RATIO = 0.25
CATEGORICAL = ('material', 'packing', 'route_type')
NUMERIC = ('max_rh', 'transfers')
COLUMNS = CATEGORICAL + NUMERIC
REGULARIZATION_C = 1.0


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def synthetic_inspections():
    """与首版相同的 960 条合成检查，作为实查样本不足时的垫底训练数据。"""
    rng = np.random.default_rng(SEED)
    rows = []
    for index in range(960):
        material = str(rng.choice(['bronze', 'paper', 'porcelain']))
        packing = str(rng.choice(['foam', 'suspension', 'double_wall']))
        route = str(rng.choice(['direct', 'transfer', 'mountain']))
        rh = int(rng.integers(38, 83))
        transfers = int(rng.integers(0, 5))
        latent = (-3.8 + {'bronze': 0, 'paper': 0.85, 'porcelain': 1.65}[material]
                  + {'foam': 1.25, 'suspension': 0, 'double_wall': 0.5}[packing]
                  + {'direct': 0, 'transfer': 0.75, 'mountain': 1.25}[route]
                  + 0.052 * (rh - 48) + 0.39 * transfers)
        label = int(rng.random() < 1 / (1 + np.exp(-latent)))
        rows.append({'loan_id': f'SYN-{index:04d}', 'finding_id': None, 'source': 'synthetic',
                     'label_version': 0, 'material': material, 'packing': packing, 'route_type': route,
                     'max_rh': rh, 'transfers': transfers, 'label': label})
    return rows


def split_groups(samples, seed=SEED, test_ratio=TEST_RATIO):
    """按借展分组、按标签分层的确定性留出划分：同一借展的样本不会同时出现在两边。"""
    groups = {}
    for index, row in enumerate(samples):
        groups.setdefault(row['loan_id'], []).append(index)
    strata = {'positive': [], 'negative': []}
    for loan_id, indices in groups.items():
        labels = {samples[i]['label'] for i in indices}
        if len(labels) > 1:
            raise ValueError(f'借展 {loan_id} 存在互相矛盾的标签，无法分组划分')
        strata['positive' if samples[indices[0]]['label'] == 1 else 'negative'].append(loan_id)
    rng = np.random.default_rng(seed)
    test_groups = set()
    for name, loan_ids in strata.items():
        ordered = np.asarray(loan_ids, dtype=object)
        rng.shuffle(ordered)
        target = round(len(ordered) * test_ratio)
        if len(ordered) > 1:
            target = min(max(target, 1), len(ordered) - 1)
        test_groups.update(ordered[:target].tolist())
    train, test = [], []
    for index, row in enumerate(samples):
        (test if row['loan_id'] in test_groups else train).append(index)
    return train, test, test_groups


def fit_encoding(rows):
    encoding, global_rate = {}, {}
    for column in CATEGORICAL:
        total = len(rows)
        positives = sum(r['label'] for r in rows)
        rate = positives / total if total else 0.0
        stats = {}
        counts = {}
        for row in rows:
            value = row[column]
            counts[value] = counts.get(value, 0) + 1
            stats[value] = stats.get(value, 0) + row['label']
        global_rate[column] = rate
        encoding[column] = {
            value: (positive + SMOOTHING * rate) / (counts[value] + SMOOTHING)
            for value, positive in stats.items()
        }
    return encoding, global_rate


def fit_scaler(rows):
    values = np.asarray([[row[column] for column in NUMERIC] for row in rows], dtype=float)
    mean = values.mean(axis=0)
    std = values.std(axis=0)
    std = np.where(std < 1e-12, 1.0, std)
    return {'mean': [float(v) for v in mean], 'std': [float(v) for v in std]}


def design_matrix(rows, encoding, global_rate, scaler):
    matrix = []
    mean = np.asarray(scaler['mean'])
    std = np.asarray(scaler['std'])
    for row in rows:
        encoded = [encoding[column].get(row[column], global_rate[column]) for column in CATEGORICAL]
        measured = (np.asarray([row[column] for column in NUMERIC], dtype=float) - mean) / std
        matrix.append([1.0, *encoded, *measured.tolist()])
    return np.asarray(matrix, dtype=float)


def fit_logistic(matrix, labels, c=REGULARIZATION_C, max_iter=100, tolerance=1e-9):
    """L2 正则 Logistic 回归的牛顿迭代；截距不做惩罚，与 sklearn 的目标函数一致。"""
    labels = np.asarray(labels, dtype=float)
    dimension = matrix.shape[1]
    beta = np.zeros(dimension)
    penalty = np.eye(dimension) / c
    penalty[0, 0] = 0.0
    for _ in range(max_iter):
        probability = 1 / (1 + np.exp(-(matrix @ beta)))
        gradient = matrix.T @ (probability - labels) + penalty @ beta
        weight = np.maximum(probability * (1 - probability), 1e-12)
        hessian = (matrix * weight[:, None]).T @ matrix + penalty
        step = np.linalg.solve(hessian, gradient)
        beta = beta - step
        if np.max(np.abs(step)) < tolerance:
            break
    return beta


def roc_auc(labels, probabilities):
    labels = np.asarray(labels)
    probabilities = np.asarray(probabilities, dtype=float)
    positive = labels == 1
    if positive.sum() in (0, len(labels)):
        return None
    order = np.argsort(probabilities, kind='mergesort')
    ranks = np.empty(len(probabilities))
    ranks[order] = np.arange(1, len(probabilities) + 1)
    values, inverse, counts = np.unique(probabilities, return_inverse=True, return_counts=True)
    rank_sum = np.zeros(len(values))
    np.add.at(rank_sum, inverse, ranks)
    averaged = (rank_sum / counts)[inverse]
    positive_count = int(positive.sum())
    return float((averaged[positive].sum() - positive_count * (positive_count + 1) / 2)
                 / (positive_count * (len(labels) - positive_count)))


def train(samples, seed=SEED):
    if not samples:
        raise ValueError('训练样本为空，无法训练模型')
    train_index, test_index, test_groups = split_groups(samples, seed)
    train_rows = [samples[i] for i in train_index]
    test_rows = [samples[i] for i in test_index]
    encoding, global_rate = fit_encoding(train_rows)
    scaler = fit_scaler(train_rows)
    matrix = design_matrix(train_rows, encoding, global_rate, scaler)
    beta = fit_logistic(matrix, [row['label'] for row in train_rows])
    probabilities = predict_rows(test_rows, encoding, global_rate, scaler, beta)
    labels = [row['label'] for row in test_rows]
    finding_count = sum(1 for row in samples if row['source'] == 'finding')
    metrics = {
        'random_seed': seed, 'risk_threshold': THRESHOLD, 'smoothing': SMOOTHING,
        'test_ratio': TEST_RATIO, 'train_count': len(train_rows), 'test_count': len(test_rows),
        'finding_count': finding_count, 'synthetic_count': len(samples) - finding_count,
        'training_source': 'inspection_findings' if finding_count else 'synthetic',
        'holdout_groups': sorted(test_groups),
        'roc_auc': round(roc_auc(labels, probabilities), 4) if probabilities else None,
        'brier_score': round(float(np.mean((np.asarray(probabilities) - np.asarray(labels)) ** 2)), 4)
        if probabilities else None,
        'regularization_c': REGULARIZATION_C, 'numpy': np.__version__,
        'training_rows_sha256': sha([{key: row[key] for key in
                                      ('loan_id', 'finding_id', 'source', 'label_version', *COLUMNS, 'label')}
                                     for row in samples]),
    }
    parameters = {
        'columns': list(COLUMNS), 'encoding': encoding, 'encoding_global': global_rate,
        'scaler': scaler, 'intercept': float(beta[0]), 'coef': [float(v) for v in beta[1:]],
        'threshold': THRESHOLD,
    }
    return parameters, metrics


def predict_rows(rows, encoding, global_rate, scaler, beta):
    matrix = design_matrix(rows, encoding, global_rate, scaler)
    return (1 / (1 + np.exp(-(matrix @ beta)))).tolist()


def score(snapshot, parameters):
    encoding, global_rate = parameters['encoding'], parameters['encoding_global']
    scaler, beta = parameters['scaler'], np.r_[parameters['intercept'], parameters['coef']]
    row = {column: snapshot[column] for column in COLUMNS}
    probability = float(predict_rows([row], encoding, global_rate, scaler, beta)[0])
    matrix = design_matrix([row], encoding, global_rate, scaler)[0]
    evidence = {
        'risk_threshold': parameters['threshold'], 'input_sha256': sha(snapshot),
        'encoded_features': dict(zip(COLUMNS, map(float, matrix[1:]), strict=True)),
    }
    return probability, 'high' if probability >= parameters['threshold'] else 'low', evidence
