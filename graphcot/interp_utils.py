from functools import partial

import matplotlib.pyplot as plt
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import torch
import tqdm.auto as tqdm_auto
import transformer_lens.utils as tl_util
# from neel_plotly import imshow, line, scatter
from sklearn.decomposition import PCA
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.neural_network import MLPClassifier

from .probing import *
from .tree_generation import generate_example
from .utils import *


def display_head(cache, labels, layer, head, show=True):
    average_patterns = cache[f"blocks.{layer}.attn.hook_pattern"]
    last_idx = average_patterns.shape[-1] - 1
    while labels[last_idx] == ",":
        last_idx -= 1
    last_idx += 1
    matrix = average_patterns[0, head, :last_idx, :last_idx].cpu()
    labels = labels[:last_idx]
    fig = px.imshow(
        matrix,
        labels=dict(x="AttendedPos", y="CurrentPos", color="Value"),
    )
    layout = dict(
        width=800,
        height=800,
        xaxis=dict(
            tickmode="array",
            tickvals=np.arange(len(labels)),
            ticktext=labels,
        ),
        yaxis=dict(
            tickmode="array",
            tickvals=np.arange(len(labels)),
            ticktext=labels,
        )
    )
    fig.update_layout(layout)
    if show:
        fig.show()
    else:
        return fig


def logits_to_logit_diff(clean_tokens, corrupted_tokens, logits, comparison_index):
    correct_index = clean_tokens[comparison_index]
    incorrect_index = corrupted_tokens[comparison_index]
    return logits[0, comparison_index-1, correct_index] - logits[0, comparison_index-1, incorrect_index]


def activation_patching(model, dataset, clean_tokens, corrupted_tokens, comparison_index):
    # We run on the clean prompt with the cache so we store activations to patch in later.
    clean_logits, clean_cache = model.run_with_cache(clean_tokens)
    clean_logit_diff = logits_to_logit_diff(clean_tokens, corrupted_tokens, clean_logits, comparison_index)
    print(f"Clean logit difference: {clean_logit_diff.item():.3f}")

    # We don't need to cache on the corrupted prompt.
    corrupted_logits = model(corrupted_tokens)
    corrupted_logit_diff = logits_to_logit_diff(clean_tokens, corrupted_tokens, corrupted_logits, comparison_index)
    print(f"Corrupted logit difference: {corrupted_logit_diff.item():.3f}")
    print(f"Positive Direction: {dataset.idx2tokens[clean_tokens[comparison_index]]}")
    print(f"Negative Direction: {dataset.idx2tokens[corrupted_tokens[comparison_index]]}")

    def residual_stream_patching_hook(
        resid_pre,
        hook,
        position):
        # Each HookPoint has a name attribute giving the name of the hook.
        clean_resid_pre = clean_cache[hook.name]
        resid_pre[:, position, :] = clean_resid_pre[:, position, :]
        return resid_pre
    # We make a tensor to store the results for each patching run. We put it on the model's device to avoid needing to move things between the GPU and CPU, which can be slow.
    num_positions = clean_tokens.shape[0]
    patching_result = torch.zeros((model.cfg.n_layers, num_positions), device=model.cfg.device)
    for layer in tqdm_auto.tqdm(range(model.cfg.n_layers)):
        for position in range(num_positions):
            # Use functools.partial to create a temporary hook function with the position fixed
            temp_hook_fn = partial(residual_stream_patching_hook, position=position)
            # Run the model with the patching hook
            patched_logits = model.run_with_hooks(corrupted_tokens, fwd_hooks=[
                (tl_util.get_act_name("resid_pre", layer), temp_hook_fn)
            ])
            # Calculate the logit difference
            patched_logit_diff = logits_to_logit_diff(clean_tokens, corrupted_tokens, patched_logits, comparison_index).detach()
            # Store the result, normalizing by the clean and corrupted logit difference so it's between 0 and 1 (ish)
            normalize_ratio = (clean_logit_diff - corrupted_logit_diff)
            if normalize_ratio == 0:
                normalize_ratio = 1
            patching_result[layer, position] = (patched_logit_diff - corrupted_logit_diff) / normalize_ratio
    return patching_result


def plot_activations(patching_result, clean_tokens, dataset):
    # Add the index to the end of the label, because plotly doesn't like duplicate labels
    token_labels = [f"{dataset.idx2tokens[token]}_{index}" for index, token in enumerate(clean_tokens)]
    imshow(patching_result, x=token_labels, xaxis="Position", yaxis="Layer", title="Activation patching")


def aggregate_activations(model, dataset, activation_keys, n_samples, path_length=None, order="backward"):
    # Collect activations for examples
    agg_cache = {ak: [] for ak in activation_keys}
    graphs = []
    for _ in range(n_samples):
        # Sample example
        test_graph = generate_example(
            n_states=dataset.n_states,
            seed=np.random.randint(1_000_000, np.iinfo(32).max),
            path_length=path_length,
            order=order
        )
        correct = is_model_correct(model, dataset, test_graph)
        if not correct:
            continue
        labels, cache = get_example_cache(test_graph, model, dataset)
        # Record information
        graphs.append(test_graph)
        for key in activation_keys:
            agg_cache[key].append(cache[key].cpu())
    return agg_cache, graphs


def logit_lens(pred, model, dataset, lenses=None):
    # Get labels and cache
    labels, cache = get_example_cache(pred, model, dataset)
    # Calculate end idx of the labels
    end = num_last(labels, ",")
    # Get the logit lens for each layer's resid_post
    outs = []
    for layer in range(1, model.cfg.n_layers+1):
        if layer < model.cfg.n_layers:
            act_name = tl_util.get_act_name("normalized", layer, "ln1")
        else:
            act_name = "ln_final.hook_normalized"
        res_stream = cache[act_name][0]
        if lenses is not None:
            out_proj = res_stream @ lenses[act_name]
        else:
            out_proj = res_stream @ model.W_U
        out_proj = out_proj.argmax(-1)
        lens_out = [dataset.idx2tokens[i] for i in out_proj]
        outs.append([f"Layer {layer} LL"] + lens_out[47:end])
    # Plot data
    header = dict(values=["Current Input"] + labels[47:end])
    rows = dict(values=np.array(outs).T.tolist())
    table = go.Table(header=header, cells=rows)
    layout = go.Layout(width=1000, height=700)
    figure = go.Figure(data=[table], layout=layout)
    figure.show()



def logit_lens_correct_probs(pred, model, dataset, position, lenses=None):
    # Get labels and cache
    labels, cache = get_example_cache(pred, model, dataset)
    # Get the probability of the correct next token at every layer
    probs = []
    correct_token = labels[position+1]
    correct_token_idx = dataset.tokens2idx[correct_token]
    for layer in range(1, model.cfg.n_layers+1):
        if layer < model.cfg.n_layers:
            act_name = tl_util.get_act_name("normalized", layer, "ln1")
        else:
            act_name = "ln_final.hook_normalized"
        res_stream = cache[act_name][0]
        if lenses is not None:
            out_proj = res_stream @ lenses[act_name]
        else:
            out_proj = res_stream @ model.W_U
        out_proj = out_proj.softmax(-1)
        probs.append( out_proj[position, correct_token_idx].item() )
    # Plot data
    plt.plot(probs)
    plt.xlabel("Layer")
    plt.ylabel(f"Probability of {correct_token}")
    plt.title(f"Probability of Correct Token at {labels[position]}")
    plt.show()
    # Return result
    return probs


def logit_lens_all_probs(pred, model, dataset, position, lenses=None):
    # Get labels and cache
    labels, cache = get_example_cache(pred, model, dataset)
    current_node = int(labels[position].split(">")[-1])
    current_neighbors = extract_adj_matrix(pred)[current_node]
    current_neighbors = [f">{i}" for i in range(dataset.n_states) if current_neighbors[i] > 0]
    # Get the logit lens for each layer's resid_post
    probs = {key: [] for key in current_neighbors}
    correct_token = labels[position+1]
    correct_token_idx = dataset.tokens2idx[correct_token]
    for layer in range(1, model.cfg.n_layers+1):
        if layer < model.cfg.n_layers:
            act_name = tl_util.get_act_name("normalized", layer, "ln1")
        else:
            act_name = "ln_final.hook_normalized"
        res_stream = cache[act_name][0]
        if lenses is not None:
            out_proj = res_stream @ lenses[act_name]
        else:
            out_proj = res_stream @ model.W_U
        out_proj = out_proj.softmax(-1)
        for key in probs:
            key_prob = out_proj[position, dataset.tokens2idx[key]].item()
            probs[key].append(key_prob)
    # Plot data
    for key in probs:
        plt.plot(probs[key], label=key)
    plt.xlabel("Layer")
    plt.ylabel(f"Probability of Token")
    plt.title(f"Probability of Correct Token at {labels[position]}")
    plt.legend()
    plt.show()
    # Return result
    return probs


def calculate_tuned_lens(model, dataset):
    # Get all the activations
    acts, graphs = aggregate_activations(
        model=model,
        dataset=dataset,
        activation_keys=[tl_util.get_act_name("normalized", block, "ln1") 
                            for block in range(1, model.cfg.n_layers)] + ["ln_final.hook_normalized"],
        n_samples=16384,
        order="random"
    )
    # Create input/output pairs
    X = {key: [] for key in acts.keys()}
    y = []
    for gidx, graph in enumerate(graphs):
        # Get output labels
        tokens = dataset.tokenize(graph)[:-1]
        start_idx = np.where(tokens == dataset.start_token)[0].item() + 2
        labels = [dataset.idx2tokens[idx] for idx in tokens]
        end_idx = num_last(labels, ",") #+ 1
        y.append(tokens[start_idx:end_idx])
        # Iterate over all layers residual streams
        for key in X.keys():
            streams = acts[key][gidx][0, start_idx-1:end_idx-1]
            X[key].append(streams)
    # Convert everything to np arrays
    for key in X.keys():
        X[key] = torch.cat(X[key], dim=0).detach().cpu().numpy()
    y = np.concatenate(y, axis=0).astype(np.int64)
    # Calculate a lens for every layer
    translators = {}
    for key in X.keys():
        tprobe = LinearClsProbe(fit_intercept=False)
        tprobe.fit(X[key], y)
        print(tprobe.score(X[key], y))
        W = tprobe.model.weight.data
        W = W.T
        translators[key] = W.to(model.cfg.device)
    return translators
