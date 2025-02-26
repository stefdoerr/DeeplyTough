import concurrent.futures
import logging
import os
import pickle
import subprocess
import tempfile
import urllib.request
from collections import defaultdict

import Bio.PDB as PDB
import numpy as np
import requests
from sklearn.metrics import precision_recall_curve, roc_curve, roc_auc_score
from sklearn.model_selection import KFold, GroupShuffleSplit

from misc.utils import mk_featurizer, voc_ap, RcsbPdbClusters, pdb_check_obsolete

logger = logging.getLogger(__name__)


class ToughM1:
    """
    TOUGH-M1 dataset by Govindaraj and Brylinski
    https://osf.io/6ngbs/wiki/home/
    """
    def __init__(self):
        self.tough_data_dir = os.path.join(os.environ.get('STRUCTURE_DATA_DIR'), 'TOUGH-M1')

    @staticmethod
    def _preprocess_worker(entry):

        def struct_to_centroid(structure):
            return np.mean(np.array([atom.get_coord() for atom in structure.get_atoms()]), axis=0)

        def pdb_chain_to_uniprot(pdb_code, query_chain_id):
            """
            Get pdb chain mapping to uniprot accession using the pdbe api
            """
            result = 'None'
            r = requests.get(f'http://www.ebi.ac.uk/pdbe/api/mappings/uniprot/{pdb_code}')
            fam = r.json()[pdb_code]['UniProt']

            for fam_id in fam.keys():
                for chain in fam[fam_id]['mappings']:
                    if chain['chain_id'] == query_chain_id:
                        if result != 'None' and fam_id != result:
                            logger.warning(f'DUPLICATE {fam_id} {result}')
                        result = fam_id
            if result == 'None':
                logger.warning(f'No uniprot accession found for {pdb_code}: {query_chain_id}')
            return result

        # 1) We won't be using provided `.fpocket` files because they don't contain the actual atoms, just
        # Voronoii centers. So we run fpocket2 ourselves, it seems to be equivalent to published results.
        try:
            command = ['fpocket2', '-f', entry['protein']]
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as e:
            logger.warning('Calling fpocket2 failed, please make sure it is on the PATH')
            raise e

        # 2) Some chains have been renamed since TOUGH-M1 dataset was released so one cannot directly retrieve
        # uniprot accessions corresponding to a given chain. So we first locate corresponding chains in the
        # original pdb files, get their ids and translate those to uniprot using the SIFTS webservices.
        parser = PDB.PDBParser(PERMISSIVE=True, QUIET=True)
        tough_str = parser.get_structure('t', entry['protein'])
        tough_c = struct_to_centroid(tough_str)

        # 2a) Some structures are now obsolete since TOUGH-M1 was published, for these, get superceding entry
        pdb_code = entry['code'].lower()
        superceded = pdb_check_obsolete(entry['code'])
        if superceded:
            pdb_code = superceded
        # 2b) try to download pdb from RSCB mirror site
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = tmpdir + '/prot.pdb'
            try:
                urllib.request.urlretrieve(f"http://files.rcsb.org/download/{pdb_code}.pdb", fname)
            except:
                logger.info(f'Could not download PDB: {pdb_code}')
                return [entry['code5'], 'None', 'None']
            orig_str = parser.get_structure('o', fname)

        # TOUGH authors haven't re-centered the chains so we can roughly find them just by centroids :)
        dists = []
        ids = []
        for model in orig_str:
            for chain in model:
                if len(chain) < 20:  # ignore chains with fewer than 20 residues
                    continue
                dists.append(np.linalg.norm(struct_to_centroid(chain) - tough_c))
                ids.append(chain.id)
        chain_id = ids[np.argmin(dists)]
        if np.min(dists) > 5:
            logger.warning(f"Suspiciously large distance when trying to map tough structure to downloaded one"
                           f"DIST {dists} {ids} {entry['code']} {pdb_code}")
            return [entry['code5'], 'None', 'None']

        uniprot = pdb_chain_to_uniprot(pdb_code.lower(), chain_id)
        return [entry['code5'], uniprot, pdb_code.lower() + chain_id]

    def preprocess_once(self):
        """
        Re-run fpocket2 and try to obtain Uniprot Accession for each PDB entry.
        Needs to be called just once in a lifetime
        """
        code5_to_uniprot = {}
        code5_to_seqclust = {}
        uniprot_to_code5 = defaultdict(list)
        logger.info('Preprocessing: obtaining uniprot accessions, this will take time.')
        entries = self.get_structures(extra_mappings=False)
        clusterer = RcsbPdbClusters(identity=30)
        
        with concurrent.futures.ProcessPoolExecutor() as executor:
            for code5, uniprot, code5new in executor.map(ToughM1._preprocess_worker, entries):
                code5_to_uniprot[code5] = uniprot
                uniprot_to_code5[uniprot] = uniprot_to_code5[uniprot] + [code5]
                code5_to_seqclust[code5] = clusterer.get_seqclust(code5new[:4], code5new[4:5])

        unclustered = [k for k,v in code5_to_seqclust.items() if v == 'None']
        if len(unclustered) > 0:
            logger.info(f"Unable to get clusters for {len(unclustered)} entries: {unclustered}")

        # write uniprot mapping to file
        pickle.dump({
                'code5_to_uniprot': code5_to_uniprot,
                'uniprot_to_code5': uniprot_to_code5,
                'code5_to_seqclust': code5_to_seqclust
            },
            open(os.path.join(self.tough_data_dir, 'pdbcode_mappings.pickle'), 'wb')
        )

        # prepare coordinates and feature channels for descriptor calculation
        mk_featurizer(self.get_structures(), skip_existing=True)

    def get_structures(self, extra_mappings=True):
        """
        Get list of PDB structures with metainfo
        """
        root = os.path.join(self.tough_data_dir, 'TOUGH-M1_dataset')
        npz_root = os.path.join(os.environ.get('STRUCTURE_DATA_DIR'), 'processed/htmd/TOUGH-M1/TOUGH-M1_dataset')
        fname_uniprot_mapping = os.path.join(self.tough_data_dir, 'pdbcode_mappings.pickle')

        # try to load translation pickle
        code5_to_uniprot = None
        code5_to_seqclust = None
        if extra_mappings:
            mapping = pickle.load(open(fname_uniprot_mapping, 'rb'))
            code5_to_uniprot = mapping['code5_to_uniprot']
            code5_to_seqclust = mapping['code5_to_seqclust']

        entries = []
        with open(os.path.join(self.tough_data_dir, 'TOUGH-M1_pocket.list')) as f:
            for line in f.readlines():
                code5, pocketnr, _ = line.split()
                entries.append({
                    'protein': root + f'/{code5}/{code5}.pdb',
                    'pocket': root + f'/{code5}/{code5}_out/pockets/pocket{int(pocketnr)-1}_vert.pqr',
                    'ligand': root + f'/{code5}/{code5}00.pdb',
                    'protein_htmd': npz_root + f'/{code5}/{code5}.npz',
                    'code5': code5,
                    'code': code5[:4],
                    'uniprot': code5_to_uniprot[code5] if code5_to_uniprot else 'None',
                    'seqclust': code5_to_seqclust[code5] if code5_to_seqclust else 'None'
                })
        return entries

    def get_structures_splits(self, fold_nr, strategy='seqclust', n_folds=5, seed=0):
        pdb_entries = self.get_structures()

        if strategy == 'pdb_folds':
            splitter = KFold(n_splits=n_folds, shuffle=True, random_state=seed)
            folds = list(splitter.split(pdb_entries))
            train_idx, test_idx = folds[fold_nr]
            return [pdb_entries[i] for i in train_idx], [pdb_entries[i] for i in test_idx]

        elif strategy == 'uniprot_folds':
            splitter = GroupShuffleSplit(n_splits=n_folds, test_size=1.0/n_folds, random_state=seed)
            pdb_entries = list(filter(lambda entry: entry['uniprot'] != 'None', pdb_entries))
            folds = list(splitter.split(pdb_entries, groups=[e['uniprot'] for e in pdb_entries]))
            train_idx, test_idx = folds[fold_nr]
            return [pdb_entries[i] for i in train_idx], [pdb_entries[i] for i in test_idx]

        elif strategy == 'seqclust':
            splitter = GroupShuffleSplit(n_splits=n_folds, test_size=1.0/n_folds, random_state=seed)
            pdb_entries = list(filter(lambda entry: entry['seqclust'] != 'None', pdb_entries))
            folds = list(splitter.split(pdb_entries, groups=[e['seqclust'] for e in pdb_entries]))
            train_idx, test_idx = folds[fold_nr]
            return [pdb_entries[i] for i in train_idx], [pdb_entries[i] for i in test_idx]

        elif strategy == 'none':
            return pdb_entries, pdb_entries
        else:
            raise NotImplementedError

    def evaluate_matching(self, descriptor_entries, matcher):
        """
        Evaluate pocket matching on TOUGH-M1 dataset. The evaluation metrics is AUC.

        :param descriptor_entries: List of entries
        :param matcher: PocketMatcher instance
        """

        target_dict = {d['code5']: d for d in descriptor_entries}
        pairs = []
        positives = []

        def parse_file_list(f):
            f_pairs = []
            for line in f.readlines():
                id1, id2 = line.split()[:2]
                if id1 in target_dict and id2 in target_dict:
                    f_pairs.append((target_dict[id1], target_dict[id2]))
            return f_pairs

        with open(os.path.join(self.tough_data_dir, 'TOUGH-M1_positive.list')) as f:
            pos_pairs = parse_file_list(f)
            pairs.extend(pos_pairs)
            positives.extend([True] * len(pos_pairs))

        with open(os.path.join(self.tough_data_dir, 'TOUGH-M1_negative.list')) as f:
            neg_pairs = parse_file_list(f)
            pairs.extend(neg_pairs)
            positives.extend([False] * len(neg_pairs))

        scores = matcher.pair_match(pairs)

        goodidx = np.flatnonzero(np.isfinite(np.array(scores)))
        if len(goodidx) != len(scores):
            logger.warning(f'Ignoring {len(scores) - len(goodidx)} pairs')
            positives_clean, scores_clean = np.array(positives)[goodidx],  np.array(scores)[goodidx]
        else:
            positives_clean, scores_clean = positives, scores

        # Calculate metrics
        fpr, tpr, roc_thresholds = roc_curve(positives_clean, scores_clean)
        auc = roc_auc_score(positives_clean, scores_clean)
        precision, recall, thresholds = precision_recall_curve(positives_clean, scores_clean)
        ap = voc_ap(recall[::-1], precision[::-1])

        results = {
            'ap': ap,
            'pr': precision,
            're': recall,
            'th': thresholds,
            'auc': auc,
            'fpr': fpr,
            'tpr': tpr,
            'th_roc': roc_thresholds,
            'pairs': pairs,
            'scores': scores,
            'pos_mask': positives
        }
        return results
