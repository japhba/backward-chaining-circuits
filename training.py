import argparse

from transformer_lens import HookedTransformer, HookedTransformerConfig

from tree_generation import GraphDataset
from utils import train, get_loaders


def main(args):
   
    # setup dataset 
    dataset = GraphDataset(args.n_states, args.dataset_file_name, args.n_samples)
    train_loader, test_loader = get_loaders(dataset, args.batch_size)

    # setup model
    cfg = HookedTransformerConfig(
        n_layers=6,
        d_model=256,
        n_ctx=dataset.max_seq_length - 1,
        n_heads=1,
        d_mlp=1024,
        d_head=256,
        d_vocab=len(dataset.idx2tokens),
        device="cuda",
        attention_dir= "causal",
        act_fn="gelu",
    )
    model = HookedTransformer(cfg)

    # optional: load model checkpoint
    checkpoint = None
    
    # start training loop
    train(model, train_loader, test_loader, n_epochs=1000, checkpoint=checkpoint, learning_rate=3e-4)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    # dataset configuration
    parser.add_argument('--n_states', default=32)
    parser.add_argument('--dataset_file_name', default="dataset.txt")
    parser.add_argument('--n_samples', default=300_000)
    parser.add_argument('--batch_size', default=32)

    # model configuration
    parser.add_argument('--n_layers', default=32)
    parser.add_argument('--d_model', default=256)
    parser.add_argument('--n_heads', default=1)
    parser.add_argument('--d_mlp', default=1024)
    parser.add_argument('--d_head', default=256)
    args = parser.parse_args()

    main(args)