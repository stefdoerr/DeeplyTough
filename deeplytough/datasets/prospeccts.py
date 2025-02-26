import os
import glob
import pickle
import requests
import string
import concurrent.futures
import numpy as np
from sklearn.metrics import precision_recall_curve, roc_curve, roc_auc_score
from misc.utils import mk_featurizer, voc_ap, RcsbPdbClusters
from misc.ligand_extract import PocketFromLigandDetector

import logging

logger = logging.getLogger(__name__)


class Prospeccts:
    """ ProSPECCTs dataset by Ehrt et al (http://www.ccb.tu-dortmund.de/ag-koch/prospeccts/) """

    dbnames = ['P1', 'P1.2', 'P2', 'P3', 'P4', 'P5', 'P5.2', 'P6', 'P6.2', 'P7']

    def __init__(self, dbname):
        self.dbname = dbname

    @staticmethod
    def _get_pdb_code_from_raw_pdb(pdbpath):
        search_string = os.path.basename(pdbpath)[:2]
        logger.info(f'searching for pdb id using string: {search_string}')
        with open(pdbpath, 'r') as f:
            g = f.readlines()
            pdb_code = None
            while pdb_code is None and len(g):
                line = g.pop(0)
                for s in line.split():
                    if search_string in s:
                        maybe_code = s[:4]
                        # check this is a real NMR pdb code
                        try:
                            logger.info(f"checking whether {maybe_code} is a real NMR entry in the PDB")
                            r = requests.get(f"https://www.ebi.ac.uk/pdbe/api/pdb/entry/experiment/{maybe_code}")
                            exp = r.json()[maybe_code][0]['experimental_method']
                        except Exception as e:
                            continue
                        # if pdb is real, and the experimental method is NMR. Eureka!
                        if "NMR" in exp:
                            pdb_code = maybe_code
        return pdb_code

    @staticmethod
    def _extract_pocket_and_get_uniprot(pdbpath):
        fname = os.path.basename(pdbpath).split('.')[0]
        if '_' in fname:
            return None, None

        # 1) Extract the pocket
        detector = PocketFromLigandDetector(include_het_resname=False, save_clean_structure=True,
                                            keep_other_hets=False, min_lig_atoms=1, allowed_lig_names=['LIG'])
        detector.run_one(pdbpath, os.path.dirname(pdbpath))

        # 2) Attempt to map to Uniprots (fails from time to time, return 'None' in that case)
        pdb_code = fname[:4].lower()
        query_chain_id = fname[4].upper() if len(fname) > 4 else ''
        result = set()

        # 2b) In the case of NMR structures, Prospeccts has incomplete PDB IDs (e.g. 'cz00A' is really '1cz2 00 A')
        # Therefore for this dataset, try to get the full PDB ID from the raw PDB text
        if "NMR_structures" in pdbpath:
            pdb_code = Prospeccts._get_pdb_code_from_raw_pdb(pdbpath)
            if not pdb_code:
                pdb_code = 'XXXX'

        try:
            r = requests.get(f'http://www.ebi.ac.uk/pdbe/api/mappings/uniprot/{pdb_code}')
            fam = r.json()[pdb_code]['UniProt']
        except Exception as e:
            # this logically fails for artificial proteins not in PDB, such as in decoys (P3, P4), but that's fine.
            logger.warning(f'PDB not found {e} {pdb_code} {query_chain_id}')
            return fname, 'None'
        for fam_id in fam.keys():
            for chain in fam[fam_id]['mappings']:
                if not query_chain_id:
                    result.add(fam_id)
                elif chain['chain_id'] == query_chain_id:
                    if len(result) > 0 and fam_id != next(iter(result)):
                        logger.warning(f'Duplicate chain {fam_id} {result}')
                    result.add(fam_id)
        if not result:
            logger.warning(f'Chain not found {pdb_code} chain {query_chain_id}')
        return fname, result

    def preprocess_once(self):
        if self.dbname == 'P1':  # batch downloading and mapping together and do it just once, e.g. with P1
            logger.info('Preprocessing: extracting pockets and obtaining uniprots, this will take time.')
            all_pdbs = glob.glob(os.environ['STRUCTURE_DATA_DIR'] + '/prospeccts/**/*.pdb', recursive=True)
            all_pdbs = [pdb for pdb in all_pdbs if (pdb.count('_site') + pdb.count('_lig') + pdb.count('_clean')) == 0]

            code5_to_seqclusts = {}
            clusterer = RcsbPdbClusters(identity=30)   

            code5_to_uniprot = {}
            with concurrent.futures.ProcessPoolExecutor() as executor:
                for code, uniprot in executor.map(Prospeccts._extract_pocket_and_get_uniprot, all_pdbs):
                    if code:
                        code5_to_uniprot[code] = uniprot
                        seqclusts = set([clusterer.get_seqclust(code[:4], c) for c in string.ascii_uppercase])
                        code5_to_seqclusts[code] = seqclusts                    

            pickle.dump({
                    'code5_to_uniprot': code5_to_uniprot,
                    'code5_to_seqclusts': code5_to_seqclusts
                },
                open(os.path.join(os.environ['STRUCTURE_DATA_DIR'], 'prospeccts', 'pdbcode_mappings.pickle'), 'wb')
            )

        mk_featurizer(self.get_structures(extra_mappings=False), skip_existing=True)

    def _prospeccts_paths(self):
        if self.dbname == 'P1':
            dir1, dir2, listfn = 'identical_structures', 'identical_structures', 'identical_structures.csv'
        elif self.dbname == 'P1.2':
            dir1, dir2, listfn = 'identical_structures_similar_ligands', 'identical_structures_similar_ligands', 'identical_structures_similar_ligands.csv'
        elif self.dbname == 'P2':
            dir1, dir2, listfn = 'NMR_structures', 'NMR_structures', 'NMR_structures.csv'
        elif self.dbname == 'P3':
            dir1, dir2, listfn = 'decoy', 'decoy_structures', 'decoy_structures5.csv'
        elif self.dbname == 'P4':
            dir1, dir2, listfn = 'decoy', 'decoy_shape_structures', 'decoy_structures5.csv'
        elif self.dbname == 'P5':
            dir1, dir2, listfn = 'kahraman_structures', 'kahraman_structures', 'kahraman_structures80.csv'
        elif self.dbname == 'P5.2':
            dir1, dir2, listfn = 'kahraman_structures', 'kahraman_structures', 'kahraman_structures.csv'
        elif self.dbname == 'P6':
            dir1, dir2, listfn = 'barelier_structures', 'barelier_structures', 'barelier_structures.csv'
        elif self.dbname == 'P6.2':
            dir1, dir2, listfn = 'barelier_structures', 'barelier_structures_cofactors', 'barelier_structures.csv'
        elif self.dbname == 'P7':
            dir1, dir2, listfn = 'review_structures', 'review_structures', 'review_structures.csv'
        else:
            raise NotImplementedError
        return dir1, dir2, listfn

    def get_structures(self, extra_mappings=True):
        """ Get list of PDB structures with metainfo """
        dir1, dir2, listfn = self._prospeccts_paths()
        root = os.path.join(os.environ.get('STRUCTURE_DATA_DIR'), 'prospeccts', dir1)
        npz_root = os.path.join(os.environ.get('STRUCTURE_DATA_DIR'), 'processed/htmd/prospeccts', dir1)

        db_pdbs = set()
        with open(os.path.join(root, listfn)) as f:
            for line in f.readlines():
                tokens = line.split(',')
                db_pdbs.add(tokens[0])
                db_pdbs.add(tokens[1])
                
        code5_to_seqclusts, code5_to_uniprot = None, None
        if extra_mappings:
            mapping = pickle.load(open(os.path.join(os.environ['STRUCTURE_DATA_DIR'], 'prospeccts', 'pdbcode_mappings.pickle'), 'rb'))
            code5_to_seqclusts = mapping['code5_to_seqclusts']
            code5_to_uniprot = mapping['code5_to_uniprot']

        entries = []
        for pdb in db_pdbs:
            entries.append({
                'protein': root + f'/{dir2}/{pdb}_clean.pdb',
                'pocket': root + f'/{dir2}/{pdb}_site_1.pdb',
                'ligand': root + f'/{dir2}/{pdb}_lig_1.pdb',
                'protein_htmd': npz_root + f'/{dir2}/{pdb}_clean.npz',
                'code5': pdb,
                'code': pdb[:4],
                'uniprot': code5_to_uniprot[pdb] if code5_to_uniprot else 'None',
                'seqclusts': code5_to_seqclusts[pdb] if code5_to_seqclusts else 'None',
            })
        return entries

    def evaluate_matching(self, descriptor_entries, matcher):
        """
        Evaluate pocket matching on one Prospeccts dataset
        The evaluation metrics is AUC

        :param descriptor_entries: List of entries
        :param matcher: PocketMatcher instance
        """

        target_dict = {d['code5']: d for d in descriptor_entries}
        pairs = []
        positives = []

        dir1, dir2, listfn = self._prospeccts_paths()
        root = os.path.join(os.environ.get('STRUCTURE_DATA_DIR'), 'prospeccts', dir1)

        with open(os.path.join(root, listfn)) as f:
            for line in f.readlines():
                tokens = line.split(',')
                id1, id2, cls = tokens[0], tokens[1], tokens[2].strip()
                if id1 in target_dict and id2 in target_dict:
                    pairs.append((target_dict[id1], target_dict[id2]))
                    positives.append(cls == 'active')
                else:
                    logger.warning(f'Detection entry missing for {id1},{id2}')

        scores = matcher.pair_match(pairs)

        goodidx = np.flatnonzero(np.isfinite(np.array(scores)))
        if len(goodidx) != len(scores):
            logger.warning(f'Ignoring {len(scores) - len(goodidx)} pairs')
            positives_clean, scores_clean = np.array(positives)[goodidx], np.array(scores)[goodidx]
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
