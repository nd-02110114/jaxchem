import os
import time
import random
import pickle
import argparse
import itertools


import numpy as np
import jax.numpy as jnp
import jax.random as jrandom
from jax import grad, jit
from jax.experimental import optimizers
from sklearn.metrics import roc_auc_score


from deepchem.molnet import load_tox21
from jaxchem.models import GCNPredicator, clipped_sigmoid
from jaxchem.utils import EarlyStopping


task_names = ['NR-AR', 'NR-AR-LBD', 'NR-AhR', 'NR-Aromatase', 'NR-ER', 'NR-ER-LBD',
              'NR-PPAR-gamma', 'SR-ARE', 'SR-ATAD5', 'SR-HSE', 'SR-MMP', 'SR-p53']


def parse_arguments():
    parser = argparse.ArgumentParser('Tox21 example')
    parser.add_argument('--seed', type=int, default=1234)
    parser.add_argument('--task', type=str, choices=task_names, default='NR-AR')
    parser.add_argument('--batch-size', type=int, default=64)
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--early-stop', type=int, default=10)
    return parser.parse_args()


def seed_everything(seed):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)


def collate_fn(original_batch, task_index, rng, is_train):
    """Make a correct batch as GCN model inputs"""
    # convert a batch returned by iterbatches to a correct batch as model inputs
    inputs, targets, _, _ = original_batch
    node_feats = np.array([inputs[i][1] for i in range(len(inputs))])
    adj = np.array([inputs[i][0] for i in range(len(inputs))])
    targets = targets[:, task_index]
    return (node_feats, adj, rng, is_train, targets)


def main():
    args = parse_arguments()
    # fix seed
    seed_everything(args.seed)

    # load tox21 dataset
    tox21_tasks, tox21_datasets, _ = load_tox21(featurizer='AdjacencyConv', reload=True)
    train_dataset, valid_dataset, test_dataset = tox21_datasets

    # define hyperparams
    rng = jrandom.PRNGKey(args.seed)
    # model params
    hidden_feats = [64, 64, 64]
    activation, batchnorm, dropout = None, None, None  # use default
    predicator_hidden_feats = 32
    pooling_method = 'mean'
    predicator_dropout = None  # use default
    n_out = 1  # binary classification
    # training params
    lr = args.lr
    num_epochs = args.epochs
    batch_size = args.batch_size
    task = args.task
    early_stop_patience = args.early_stop

    # setup model
    init_fun, predict_fun = \
        GCNPredicator(hidden_feats=hidden_feats, activation=activation, batchnorm=batchnorm,
                      dropout=dropout, pooling_method=pooling_method,
                      predicator_hidden_feats=predicator_hidden_feats,
                      predicator_dropout=predicator_dropout, n_out=n_out)

    # init params
    rng, init_key = jrandom.split(rng)
    sample_node_feat = train_dataset.X[0][1]
    input_shape = sample_node_feat.shape
    _, init_params = init_fun(init_key, input_shape)
    opt_init, opt_update, get_params = optimizers.adam(step_size=lr)
    opt_state = opt_init(init_params)

    @jit
    def predict(params, inputs):
        """Predict the logits"""
        preds = predict_fun(params, *inputs)
        logits = clipped_sigmoid(preds)
        return logits

    # define training loss
    @jit
    def loss(params, batch):
        """Compute the loss (binary cross entropy) """
        inputs, targets = batch[:-1], batch[-1]
        logits = predict(params, inputs)
        loss = -jnp.mean(targets * jnp.log(logits) + (1 - targets) * jnp.log(1 - logits))
        return loss

    # define training update
    @jit
    def update(i, opt_state, batch):
        """Update the params"""
        params = get_params(opt_state)
        return opt_update(i, grad(loss)(params, batch), opt_state)

    print("Starting training...")
    task_index = tox21_tasks.index(task)
    itercount = itertools.count()
    early_stop = EarlyStopping(patience=early_stop_patience)
    for epoch in range(num_epochs):
        # train
        start_time = time.time()
        for original_batch in train_dataset.iterbatches(batch_size=batch_size):
            rng, key = jrandom.split(rng)
            batch = collate_fn(original_batch, task_index, key, True)
            opt_state = update(next(itercount), opt_state, batch)
        epoch_time = time.time() - start_time

        # valid
        params = get_params(opt_state)
        y_score, y_true, valid_loss = [], [], []
        for original_batch in valid_dataset.iterbatches(batch_size=batch_size):
            rng, key = jrandom.split(rng)
            batch = collate_fn(original_batch, task_index, key, False)
            y_score.extend(predict(params, batch[:-1]))
            y_true.extend(batch[-1])
            valid_loss.append(loss(params, batch))
        score = roc_auc_score(y_true, y_score)

        # log
        print(f"Iter {epoch}/{num_epochs} ({epoch_time:.4f} s) valid loss: {np.mean(valid_loss):.4f} \
            valid roc_auc score: {score:.4f}")
        # check early stopping
        early_stop.update(score, params)
        if early_stop.is_train_stop:
            print("Early stopping...")
            break

    # test
    y_score, y_true = [], []
    best_params = early_stop.best_params
    for original_batch in test_dataset.iterbatches(batch_size=batch_size):
        rng, key = jrandom.split(rng)
        batch = collate_fn(original_batch, task_index, key, False)
        y_score.extend(predict(best_params, batch[:-1]))
        y_true.extend(batch[-1])
    score = roc_auc_score(y_true, y_score)
    print(f'Test roc_auc score: {score:.4f}')
    # save best params
    with open('./best_params.pkl', 'wb') as f:
        pickle.dump(best_params, f)


if __name__ == "__main__":
    main()
