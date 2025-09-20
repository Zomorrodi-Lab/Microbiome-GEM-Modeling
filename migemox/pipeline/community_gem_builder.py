"""
Model Builder for MiGEMox Pipeline

This module contains functions dedicated to the construction, modification,
and preparation of microbiome community models. It handles the integration
of individual microbe GEMs, the addition of host-microbiome compartments,
the creation of community biomass reactions, and the pruning of models
based on sample-specific abundances.
"""

import cobra
from cobra import Reaction, Metabolite
from scipy.io import savemat
import pandas as pd
import os
import re
from migemox.pipeline.constraints import build_global_coupling_constraints, prune_coupling_constraints_by_microbe
from migemox.pipeline.io_utils import make_community_gem_dict
from concurrent.futures import ProcessPoolExecutor
from tqdm import tqdm

# Metabolite exchange bounds (mmol/gDW/h)
EXCHANGE_BOUNDS = (-1000.0, 10000.0) # Max uptake and secretion rates

# Transport reaction bounds (mmol/gDW/h) 
TRANSPORT_BOUNDS = (0.0, 10000.0) # Unidirectional transport

# microbe inclusion threshold
ABUNDANCE_THRESHOLD = 1e-7

def create_rxn(rxn_identifier: str, name: str, subsystem: str, bounds: tuple) -> cobra.Reaction:
    """
    Create a COBRA reaction with specified bounds and metadata.
    
    Args:
        rxn_identifier: Unique reaction ID
        name: Human-readable reaction name
        subsystem: Metabolic subsystem classification
        bounds: Lower and upper bounds of reaction (mmol/gDW/h)
        
    Returns:
        Configured COBRA reaction object
    """
    rxn = Reaction(rxn_identifier)
    rxn.name = name
    rxn.subsystem = subsystem
    lb, ub = bounds
    rxn.lower_bound = lb
    rxn.upper_bound = ub
    return rxn

def add_diet_fecal_compartments(model: cobra.Model) -> cobra.Model:
    """
    Add diet and fecal compartments to community model for host interaction.
    
    Biological System Modeled:
    - Diet compartment [d]: Nutrients from host dietary intake
    - Lumen compartment [u]: Shared microbial metabolite pool  
    - Fecal compartment [fe]: Metabolites excreted from host system
    
    Transport Chain: Diet[d] → DUt → Lumen[u] → UFEt → Fecal[fe] → EX_
    
    This creates the host-microbiome metabolite exchange interface essential
    for modeling dietary interventions and metabolite production.

    For every general metabolite in the lumen, 4 reactions will be added:
        (diet)
        EX_2omxyl[d]: 2omxyl[d] <=>
        DUt_2omxyl: 2omxyl[d] <=> 2omxyl[u]
        
        (fecal)
        UFEt_2omxyl: 2omxyl[u] <=> 2omxyl[fe]
        EX_2omxyl[fe]: 2omxyl[fe] <=>
    
    Args:
        model: Community model with microbe-tagged reactions
        
    Returns:
        Model with diet and fecal compartments and exchange and transport reactions
    """
    # Delete all EX_ reaction artifacts from the single cell models
    # E.g., EX_dad_2(e): dad_2[e] <=>, EX_thymd(e): thymd[e] <=>
    to_remove = [r for r in model.reactions if "_EX_" in r.id or "(e)" in r.id]
    model.remove_reactions(to_remove)

    # Create the diet and fecal compartments for reactions and metabolites
    # Get all of our general extracellular metabolites
    general_mets = []
    for reac in model.reactions:
        if "IEX" in reac.id:
            iex_reac = model.reactions.get_by_id(reac.id)
            # Pick only general (unlabeled) metabolites on the LHS
            for met in iex_reac.reactants:
                if "[u]" in met.id:
                    general_mets.append(met.id)
    general_mets = set(general_mets)

    # Create diet and fecal compartments, with new transport and exchange reactions
    existing_mets = {m.id for m in model.metabolites}
    existing_rxns = {r.id for r in model.reactions}
    
    for lumen_met in general_mets:
        base_name = lumen_met.split('[')[0]  # Remove [u] suffix

        # EX_2omxyl[d]: 2omxyl[d] <=>
        _add_exchange_reaction(base_name, existing_mets, model, EXCHANGE_BOUNDS, "d", "diet")
        # DUt_4hbz: 4hbz[d] --> 4hbz[u]
        _add_transport_reaction(f'DUt_{base_name}', existing_rxns, model, f'{base_name}[d]', lumen_met, TRANSPORT_BOUNDS, "diet to lumen")
        # EX_4abut[fe]: 4abut[fe] <=>
        _add_exchange_reaction(base_name, existing_mets, model, EXCHANGE_BOUNDS, "fe", "fecal")
        # UFEt_arabinoxyl: arabinoxyl[u] --> arabinoxyl[fe]
        _add_transport_reaction(f'UFEt_{base_name}', existing_rxns, model, lumen_met, f'{base_name}[fe]', TRANSPORT_BOUNDS, "lumen to fecal")

    return model

def _add_exchange_reaction(base_name: str, existing_met_ids: set, model: cobra.Model, bounds: tuple, compartment: str, label: str):
    """
    Helper function to add an exchange reaction and its associated metabolite to the model.
    """
    met_id = f'{base_name}[{compartment}]'
    if met_id not in existing_met_ids:
        reac_id = "EX_" + met_id
        reaction = create_rxn(reac_id, f"{met_id} {label} exchange", ' ', bounds)
        model.add_reactions([reaction])
        model.add_metabolites([Metabolite(met_id, compartment=compartment)])
        reaction = model.reactions.get_by_id(reac_id)
        reaction.add_metabolites({model.metabolites.get_by_id(met_id): -1})

def _add_transport_reaction(rxn_id: str, existing_rxn_ids: set, model: cobra.Model, reactant_id: str, product_id: str, bounds: tuple, label: str):
    """
    Helper function to add a transport reaction between two metabolites.
    """
    if rxn_id not in existing_rxn_ids:
        reaction = create_rxn(rxn_id, f"{rxn_id} {label} transport", ' ', bounds)
        model.add_reactions([reaction])
        reaction.reaction = f"{reactant_id} --> {product_id}"
        reaction.bounds = bounds

def com_biomass(model: cobra.Model, abun_path: str, sample_com: str) -> cobra.Model:
    """
    Create weighted community biomass reaction based on microbe abundances.
    
    Biological Equation: Community_Biomass = Σ(abundance_i × microbe_biomass_i)
    
    This represents the total microbial biomass production weighted by each
    microbe' relative abundance in the sample, creating a community-level
    growth objective that reflects the natural composition.
    
    Args:
        model: Community model with individual microbe biomass reactions
        abundance_path: Path to microbe abundance CSV file
        sample_name: Column name in abundance file for this sample
        
    Returns:
        Model with community biomass reaction and transport to fecal compartment
    """

    # Deleting all previous community biomass equations
    biomass_reactions = [r for r in model.reactions if "Biomass" in r.id]
    model.remove_reactions(biomass_reactions)

    # Load abundance data and filter by threshold
    abun_df = pd.read_csv(abun_path)
    abun_df = abun_df[abun_df[sample_com] > ABUNDANCE_THRESHOLD]

    # Creating the community biomass reaction
    reaction = create_rxn("communityBiomass", "communityBiomass", ' ', (0., 10000.))
    model.add_reactions([reaction])
    community_biomass = model.reactions.communityBiomass

    # Build abundance-weighted biomass stoichiometry
    biomass_stoichiometry = {}
    
    for _, row in abun_df.iterrows():
        microbe_name = row["X"]
        abundance = float(row[sample_com])
        biomass_met_id = f"{microbe_name}_biomass[c]"
        if biomass_met_id in model.metabolites:
            biomass_stoichiometry[biomass_met_id] = -abundance
        else:
            print(f"⚠️ Biomass metabolite missing in model: {biomass_met_id}")
    
    community_biomass.add_metabolites(metabolites_to_add=biomass_stoichiometry, combine=True)

    # Adding the microbeBiomass metabolite
    model.add_metabolites([Metabolite("microbeBiomass[u]", formula=" ", \
                                      name="product of community biomass", compartment="u"),])
    community_biomass.add_metabolites({model.metabolites.get_by_id("microbeBiomass[u]"): 1})

    # Adding the exchange reaction compartment
    reac_name = "EX_microbeBiomass[fe]"
    reaction = create_rxn(reac_name, reac_name, ' ', (-10000., 10000.))
    model.add_reactions([reaction])
    model.add_metabolites([Metabolite("microbeBiomass[fe]", formula=" ", \
                                      name="product of community biomass", compartment="fe"),])
    new_fe_react = model.reactions.get_by_id("EX_microbeBiomass[fe]")
    new_fe_react.add_metabolites({model.metabolites.get_by_id("microbeBiomass[fe]"): -1})

    # Adding the UFEt reaction
    reaction = create_rxn("UFEt_microbeBiomass", "UFEt_microbeBiomass", ' ', TRANSPORT_BOUNDS)
    model.add_reactions([reaction])
    reaction.reaction = "microbeBiomass[u] --> microbeBiomass[fe]"
    reaction.bounds = TRANSPORT_BOUNDS

    return model

def tag_metabolite(met: cobra.Metabolite, microbe_name: str, compartment: str):
    '''
    Helper function for microbe_to_community for tagging metabolites
    '''
    met.compartment = compartment
    no_c_name = met.id.replace(f"[{compartment}]", "")
    met.id = f'{microbe_name}_{no_c_name}[{compartment}]'

def reformat_gem_for_community(model: cobra.Model, microbe_model_name: str):
    """
    Takes a single cell AGORA GEM and changes its reaction and metabolite formatting so it 
    can be added to a community model in the Com_py pipeline.
  
        Tags everything intracellular and intracellular to extracellular with the microbe name:
            (intracellular)
            tag[c] -> tag[c]

            (transport)
            tag[c] -> tag[u]

            (IEX reactions)
            tagged[e] -> general[e]
  
    INPUTS:
        model: a .mat file of an AGORA single cell model
        microbe_model_name: the microbe name to be tagged (extracted in the Com_py pipeline)
  
    OUTPUTS:
        model: updated model with tagged reactions and metabolites
    """
    # Tagging all reactions and metabolites in the cell with microbe name
    # For each microbe model, iterate through its reactions and add the microbe tag

    # Extracting the microbe name from the microbe model name
    short_microbe_name = os.path.splitext(os.path.basename(microbe_model_name))[0]

    # Step 1: Remove all exchange reactions except for the biomass reaction
    ex_rxns = [rxn for rxn in model.reactions if "EX_" in rxn.id and "biomass" not in rxn.id]
    model.remove_reactions(ex_rxns)

    # Step 2: Tag metabolites in intra- and extracellular compartments of model
    for rxn in model.reactions:
        # Change the intracellualr reaction from [c] --> [c]
        if "[e]" in rxn.reaction or "[c]" in rxn.reaction:
            rxn.id = f'{short_microbe_name}_{rxn.id}'
            # tag each metabolite in rxn, if not already tagged
            for met in rxn.metabolites:
                if ("[c]" in met.id or "[p]" in met.id) and short_microbe_name not in met.id:
                    compartment = 'c' if '[c]' in met.id else 'p'
                    tag_metabolite(met, short_microbe_name, compartment)
                elif "[e]" in met.id and short_microbe_name not in met.id:
                    met.compartment = "u"
                    no_c_name = met.id.replace("[e]", "")
                    met.id = f'{short_microbe_name}_{no_c_name}[u]'

    # Step 3: Create inter-microbe metabolite exchange
    model = _create_inter_microbe_exchange(model, short_microbe_name)

    # Step 4: Ensure all components are properly tagged
    model = _finalize_microbe_tagging(model, short_microbe_name)

    return model

def _create_inter_microbe_exchange(model: cobra.Model, microbe_name: str) -> cobra.Model:
    """
    Create IEX reactions for microbe-specific ↔ general metabolite exchange.
    
    Biological Rationale: Allows microbe to contribute/consume shared metabolites
    in community lumen while maintaining microbe-specific uptake kinetics.
    """
    microbe_lumen_metabolites = [
        met for met in model.metabolites 
        if "[u]" in met.id and microbe_name in met.id
    ]
    
    for microbe_met in microbe_lumen_metabolites:
        general_met_id = microbe_met.id.replace(f"{microbe_name}_", "")
        
        # Create general metabolite if it doesn't exist
        if general_met_id not in [m.id for m in model.metabolites]:
            general_met = Metabolite(
                general_met_id, 
                compartment="u", 
                name=general_met_id.split("[")[0]
            )
            model.add_metabolites([general_met])
        
        # Create IEX reaction: general_met <=> microbe_met
        iex_rxn_id = f"{microbe_name}_IEX_{general_met_id}tr"
        iex_rxn = create_rxn(iex_rxn_id, f"{microbe_name}_IEX", " ", (-1000.0, 1000.0))
        model.add_reactions([iex_rxn])
        iex_rxn.reaction = f"{general_met_id} <=> {microbe_met.id}"
        iex_rxn.bounds = (-1000.0, 1000.0)
    
    return model

def _finalize_microbe_tagging(model: cobra.Model, microbe_name: str) -> cobra.Model:
    """Ensure all reactions and metabolites are properly tagged with microbe name."""
    # Tag any remaining untagged reactions
    for rxn in model.reactions:
        if not rxn.id.startswith(microbe_name):
            rxn.id = f"{microbe_name}_{rxn.id}"
    
    # Tag any remaining untagged [c] and [p] metabolites
    for met in model.metabolites:
        if ("[c]" in met.id or "[p]" in met.id) and microbe_name not in met.id:
            compartment = 'c' if '[c]' in met.id else 'p'
            tag_metabolite(met, microbe_name, compartment)
    
    return model

def prune_zero_abundance_microbe(model: cobra.Model, zero_abundance_microbe: list[str]) -> cobra.Model:
    """
    Remove all reactions and metabolites from microbe below abundance threshold.
    Biological Rationale: microbe below detection limit (typically 0.01% relative
    abundance) don't contribute meaningful metabolic flux to community phenotype.

    Args:
        model: Community model containing all microbe
        zero_abundance_microbe: microbe names below abundance threshold
        
    Returns:
        Pruned model containing only detected microbe
    """
    print('Pruning metabolites and Reactions from Zero-Abundance microbe in Sample')
    zero_prefixes = {f"{microbe}_" for microbe in zero_abundance_microbe}
    metabolites_to_remove = [
        met for met in model.metabolites 
        if any(met.id.startswith(prefix) for prefix in zero_prefixes)
    ]
    # Destructive removal also removes associated reactions automatically
    model.remove_metabolites(metabolites_to_remove, destructive=True)
    print(f"Pruned {len(metabolites_to_remove)} Metabolites")
    return model

def build_global_gem(abundance_df: pd.DataFrame, mod_dir: str) -> tuple:
    """
    Loads all microbe found in the abundance table and builds a unified, unpruned community model.
    microbe are combined into a single COBRA model, tagged and merged. Cleans the community as well
    by adding the diet and fecal compartments. This function also returns the organism models it
    loaded for later steps.

    Parameters:
        abundance_df: microbe x samples abundance dataframe.
        mod_dir: path to folder containing AGORA .mat files.

    Returns:
        tuple: Contains the cleaned global_model, and its associated 
        coupling matrices (C, d, dsense, ctrs). Also returns list
        of extracellular metabolites ([e]) found in the model.
    """

    print("Building global community model".center(40, '*'))
    all_microbe = abundance_df.index.tolist()
    first_path = os.path.join(mod_dir, all_microbe[0] + ".mat")
    print(f"Added first microbe model: {all_microbe[0]}".center(40, '*'))
    first_model = cobra.io.load_matlab_model(first_path)
    # Collect [e] metabolites from first model
    ex_mets = set()
    ex_mets.update([met.id for met in first_model.metabolites if met.id.endswith('[e]')])

    global_model = reformat_gem_for_community(first_model, microbe_model_name=first_path)
    for microbe in all_microbe[1:]:
        microbe_path = os.path.join(mod_dir, microbe + ".mat")
        model = cobra.io.load_matlab_model(microbe_path)
        ex_mets.update([met.id for met in model.metabolites if met.id.endswith('[e]')])
        tagged_model = reformat_gem_for_community(model, microbe_path)
        # Avoid duplicate reaction IDs
        existing_rxns = {r.id for r in global_model.reactions}
        new_rxns = [r for r in tagged_model.reactions if r.id not in existing_rxns]
        global_model.add_reactions(new_rxns)

    print("Finished adding GEM reconstructions to community".center(40, '*'))

    print("Adding diet and fecal compartments".center(40, '*'))
    clean_model = add_diet_fecal_compartments(model=global_model)
    print("Done adding diet and fecal compartments".center(40, '*'))

    global_C, global_d, global_dsense, global_ctrs = build_global_coupling_constraints(clean_model, all_microbe)

    return clean_model, global_C, global_d, global_dsense, global_ctrs, list(sorted(ex_mets))

def build_sample_gem(sample_name: str, global_model: cobra.Model, abundance_df: pd.DataFrame, 
                       abun_path: str, out_dir: str, global_C=None, global_d=None,
                       global_dsense=None, global_ctrs=None) -> str:
    """
    Takes a deep copy of the global model and builds the sample-specific model:
        - prunes zero-abundance microbe
        - adds diet constraints
        - adds community biomass
        - saves as .json

    Parameters:
        sample_name: column name in abundance_df
        global_model: unpruned community model
        abundance_df: pandas dataframe of abundances
        abun_path: path to abundance CSV (needed by com_biomass)
        out_dir: directory to save the output model

    Returns:
        Path to the saved model
    """
    save_path = os.path.join(out_dir, f"microbiota_model_samp_{sample_name}.mat")
    if os.path.exists(save_path):
        print(f"Personalized Model for {sample_name} already exists. Skipping.")
        return save_path
    model = global_model.copy()
    model.name = sample_name
    sample_abun = abundance_df[sample_name]

    # Prune zero-abundance microbe from the model
    zero_microbe = [sp for sp in sample_abun.index if sample_abun[sp] < 1e-7]
    present_microbe = [sp for sp in sample_abun.index if sample_abun[sp] >= 1e-7]

    model = prune_zero_abundance_microbe(model, zero_abundance_microbe=zero_microbe)

    # Add a community biomass reaction to the model
    print("Adding community biomass reaction".center(40, '*'))
    model = com_biomass(model=model, abun_path=abun_path, sample_com=sample_name)

    # Prune coupling constraints from the global model (C, dsense, d, ctrs)
    sample_C, sample_d, sample_dsense, sample_ctrs = prune_coupling_constraints_by_microbe(
        global_model, global_C, global_d, global_dsense, global_ctrs, present_microbe, model
    )

    # Ensuring the reversablity fits all compartments
    for reac in [r for r in model.reactions if "DUt" in r.id or "UFEt" in r.id]:
        reac.lower_bound = 0.

    # Setting EX_microbeBiomass[fe] as objective to match MATLAB mgPipe
    model.objective = "EX_microbeBiomass[fe]"

    os.makedirs(out_dir, exist_ok=True)
    model_dict = make_community_gem_dict(
        model, C=sample_C, d=sample_d, dsense=sample_dsense, ctrs=sample_ctrs
    )
    savemat(save_path, {'model': model_dict}, do_compression=True, oned_as='column')

    return save_path

def community_gem_builder(abun_filepath: str, mod_filepath: str, out_filepath: str, workers=1) -> tuple:
    """
    Inspired by mgpipe.m code.
    Main pipeline which inputs the GEMs data and accesses the different functions.

    INPUTS:
        abun_path: path to the microbe abundance .csv file
            Formatting for the microbe abundance:
                The columns should have the names of the .mat files of the microbe you want to load
                See file normCoverage_smaller.csv for template example   
        modpath: path to folder with all AGORA models 
            E.g. "~/data_input/AGORA103/"
        respath: path where the community models will be output (defaults to same folder as )
            E.g. "~/results/"
        dietpath: path to the AGORA compatible diet (for community model) .csv file
            E.g. "~/data_input/AverageEU_diet_fluxes.csv"
  
    OUTPUTS:
        All sample community models to a specified local folder
        tuple: (clean_samp_names, microbe_names, ex_mets)
            clean_samp_names: List of cleaned sample names (valid Python identifiers)
            microbe_names: List of microbe names in the model
            ex_mets: List of extracellular metabolites in the model
    """

    print("Starting MiGeMox pipeline".center(40, '*'))
    print("Reading abundance file".center(40, '*'))

    sample_info = pd.read_csv(abun_filepath)
    sample_info.rename(columns={list(sample_info)[0]:"microbe"}, inplace=True)
    sample_info.set_index("microbe", inplace=True)
    samp_names = list(sample_info.columns)

    clean_samp_names = []
    for name in samp_names:
        if not name.isidentifier():
            name = re.sub(r'\W', '_', name)
            if not name[0].isalpha():
                name = 'sample_' + name
        clean_samp_names.append(name)

    global_model, global_C, global_d, global_dsense, global_ctrs, ex_mets = build_global_gem(sample_info, mod_filepath)
    samples = sample_info.columns.tolist()

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(build_sample_gem, s, global_model, sample_info, abun_filepath, out_filepath, 
                                   global_C, global_d, global_dsense, global_ctrs)
                   for s in samples]
        for f in tqdm(futures, desc='Building sample GEMs'):
            f.result()
    
    return clean_samp_names, sample_info.index.tolist(), ex_mets